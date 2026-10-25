"""
PROJECT : Credit Risk Model Development (IFRS 9) - Python version
MODELS  : PD (logistic regression), LGD (OLS on recovery rate), EAD (CCF segments)
OUTPUT  : Loan-level and portfolio-level Expected Credit Loss (ECL)

This is a migration of credit_risk_model.sas. Section numbers match the SAS file.

HOW TO RUN
    pip install -r requirements.txt
    python python/credit_risk_model.py        (run from the repository root)
Folder layout (paths are resolved automatically, nothing to edit):
    python/credit_risk_model.py
    data/pd_loan_data.csv, data/lgd_data.csv, data/ead_data.csv
    output/, models/   (created automatically)

SAS -> PYTHON MAP
    DATA step INFILE            -> pandas.read_csv
    PROC SQL                    -> pandas filtering / merge / groupby
    PROC STDIZE REPONLY MEDIAN  -> fillna(median)
    PROC SURVEYSELECT (strata)  -> sklearn train_test_split(stratify=...)
    PROC LOGISTIC (stepwise)    -> statsmodels Logit + stepwise function below
    PROC REG                    -> statsmodels OLS (+ VIF, standardised betas)
    PROC MEANS / RANK           -> groupby / qcut
    PROC EXPORT                 -> DataFrame.to_csv
"""
from pathlib import Path
import pickle

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy import stats
from sklearn.metrics import roc_auc_score, roc_curve
from sklearn.model_selection import train_test_split
from statsmodels.stats.outliers_influence import variance_inflation_factor
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ----------------------------------------------------------------------
# 0. SETUP - global parameters
# ----------------------------------------------------------------------
BASE = Path(__file__).resolve().parent.parent   # repository root
DATA_DIR, OUT_DIR, MODEL_DIR = BASE / "data", BASE / "output", BASE / "models"
for d in (OUT_DIR, MODEL_DIR):
    d.mkdir(exist_ok=True)

SEED = 2026          # reproducible train/validation split
LGD_FLOOR = 0.05     # minimum LGD (5%)
SICR_PD = 0.30       # 12m PD at/above this -> Stage 2
MIN_SEG_N = 5        # min observations for a CCF segment
SLENTRY, SLSTAY = 0.30, 0.20   # stepwise entry / stay p-values

pd.set_option("display.width", 200, "display.max_columns", 50)


def banner(title):
    print("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70)


# ----------------------------------------------------------------------
# 1. DATA EXTRACTION
# ----------------------------------------------------------------------
# Oracle / Teradata code is COMMENTED OUT on purpose: it needs a live
# warehouse, credentials and drivers (oracledb, teradatasql) that a demo does
# not have. The three CSV files stand in for the database tables and produce
# the same DataFrames (raw_pd, raw_lgd, raw_ead) with the same column names,
# so every later step works identically with either source.
#
# To switch later: un-comment, fix table names, read credentials from
# environment variables (never hard-code passwords), and drop the CSV reads.
#
# import os, oracledb, teradatasql
# with oracledb.connect(user=os.environ["ORA_USER"], password=os.environ["ORA_PWD"],
#                       dsn=os.environ["ORA_DSN"]) as con:
#     raw_pd = pd.read_sql("""select l.loan_id, ... from loan_master l
#                             join customer c on c.customer_id = l.customer_id
#                             join loan_performance p on p.loan_id = l.loan_id
#                             where p.snapshot_date = date '2026-09-30'""", con)
# with teradatasql.connect(host=os.environ["TD_HOST"], user=os.environ["TD_USER"],
#                          password=os.environ["TD_PWD"]) as con:
#     raw_lgd = pd.read_sql("select ... from dw.defaulted_loans_workout", con)
#     raw_ead = pd.read_sql("select ... from dw.defaulted_facilities", con)

banner("1. DATA EXTRACTION")
raw_pd = pd.read_csv(DATA_DIR / "pd_loan_data.csv", parse_dates=["open_date", "snapshot_date"])
raw_lgd = pd.read_csv(DATA_DIR / "lgd_data.csv", parse_dates=["default_date"])
raw_ead = pd.read_csv(DATA_DIR / "ead_data.csv", parse_dates=["default_date"])
print("rows:", len(raw_pd), len(raw_lgd), len(raw_ead))

print("\nData quality profile - PD data")
num_cols = ["age", "employment_years", "annual_income", "credit_limit", "outstanding_balance",
            "monthly_debt_payment", "current_dpd", "default_flag"]
print(raw_pd[num_cols].agg(["count", "mean", "min", "max"]).T.assign(nmiss=raw_pd[num_cols].isna().sum()))

# ----------------------------------------------------------------------
# 2. DATA CLEANING
# ----------------------------------------------------------------------
banner("2. DATA CLEANING")
stg_pd = (raw_pd.drop_duplicates()
          .query("loan_id.notna() and credit_limit > 0 and outstanding_balance >= 0 "
                 "and 18 <= age <= 80 and default_flag in (0, 1)", engine="python")
          .copy())
stg_lgd = (raw_lgd.drop_duplicates()
           .query("loan_id.notna() and exposure_at_default > 0 and recovery_amount >= 0", engine="python")
           .copy())
stg_ead = (raw_ead.drop_duplicates()
           .query("facility_id.notna() and credit_limit > 0", engine="python").copy())
print("clean rows:", len(stg_pd), len(stg_lgd), len(stg_ead))

# flag + median-impute missing values
stg_pd["miss_income_flag"] = stg_pd["annual_income"].isna().astype(int)
stg_pd["miss_emp_flag"] = stg_pd["employment_years"].isna().astype(int)
for c in ["annual_income", "employment_years"]:
    stg_pd[c] = stg_pd[c].fillna(stg_pd[c].median())
pd_clean = stg_pd

# ----------------------------------------------------------------------
# 3. FEATURE ENGINEERING (borrower level)
# ----------------------------------------------------------------------
banner("3. FEATURE ENGINEERING")
f = pd_clean.copy()
f["utilization"] = np.minimum(f["outstanding_balance"] / f["credit_limit"], 1.5)
f["dti"] = np.minimum(f["monthly_debt_payment"] * 12 / f["annual_income"], 1.0)
# SAS INTCK('month', a, b) counts month boundaries crossed
f["account_age_months"] = ((f["snapshot_date"].dt.year - f["open_date"].dt.year) * 12
                           + (f["snapshot_date"].dt.month - f["open_date"].dt.month))
f["total_delinq_12m"] = f[["num_delinq_30_12m", "num_delinq_60_12m", "num_delinq_90_12m"]].sum(axis=1)
f["delinq_severity"] = (f["num_delinq_30_12m"] + 2 * f["num_delinq_60_12m"] + 3 * f["num_delinq_90_12m"])
f["ever_delinq_flag"] = (f["total_delinq_12m"] > 0).astype(int)
f["log_income"] = np.log(f["annual_income"])
f["undrawn_amount"] = np.maximum(f["credit_limit"] - f["outstanding_balance"], 0)
f["coverage_ratio"] = np.where((f["secured_flag"] == 1) & (f["outstanding_balance"] > 0),
                               np.minimum(f["collateral_value"] / f["outstanding_balance"], 1.5), 0.0)
f["util_band"] = np.where(f["utilization"] < 0.5, "LOW", "HIGH")
pd_features = f
print(pd_features[["utilization", "dti", "account_age_months", "total_delinq_12m",
                   "delinq_severity"]].describe().loc[["count", "mean", "std", "min", "max"]].T)

# ----------------------------------------------------------------------
# 4. PD MODEL - logistic regression
# ----------------------------------------------------------------------
banner("4. PD MODEL")
# 4.1 stratified 70/30 split
pd_train, pd_valid = train_test_split(pd_features, test_size=0.30, random_state=SEED,
                                      stratify=pd_features["default_flag"])
print("train/valid:", len(pd_train), len(pd_valid),
      "| defaults:", pd_train.default_flag.sum(), pd_valid.default_flag.sum())

# candidate effects; marital_status is a CLASS effect (reference = Single)
EFFECTS = {
    "utilization": ["utilization"], "dti": ["dti"],
    "account_age_months": ["account_age_months"], "delinq_severity": ["delinq_severity"],
    "age": ["age"], "employment_years": ["employment_years"], "log_income": ["log_income"],
    "marital_status": ["marital_status_Divorced", "marital_status_Married"],
}


def design(df):
    X = df.copy()
    X["marital_status_Divorced"] = (X["marital_status"] == "Divorced").astype(float)
    X["marital_status_Married"] = (X["marital_status"] == "Married").astype(float)
    return X


def fit_logit(X, y, cols):
    return sm.Logit(y, sm.add_constant(X[cols].astype(float)) if cols else
                    np.ones((len(y), 1))).fit(disp=0, maxiter=200)


def stepwise_logit(X, y, effects, slentry=SLENTRY, slstay=SLSTAY):
    """Forward-backward stepwise selection using likelihood-ratio tests.
    (SAS uses score tests for entry and Wald tests for removal, so p-values and
    the chosen model can differ slightly.)"""
    selected, log = [], []
    cols = lambda eff: [c for e in eff for c in effects[e]]
    for _ in range(50):
        changed = False
        base_ll = fit_logit(X, y, cols(selected)).llf
        cand = []
        for e in effects:
            if e in selected:
                continue
            try:
                m = fit_logit(X, y, cols(selected + [e]))
                lr = 2 * (m.llf - base_ll)
                cand.append((stats.chi2.sf(lr, len(effects[e])), e))
            except Exception:
                continue
        if cand:
            p, e = min(cand)
            if p < slentry:
                selected.append(e); changed = True
                log.append(f"Step: entered {e} (p={p:.4f})")
        while selected:                       # removal check
            full = fit_logit(X, y, cols(selected))
            worst = None
            for e in selected:
                red = fit_logit(X, y, cols([s for s in selected if s != e]))
                p = stats.chi2.sf(2 * (full.llf - red.llf), len(effects[e]))
                if worst is None or p > worst[0]:
                    worst = (p, e)
            if worst[0] >= slstay:
                selected.remove(worst[1]); changed = True
                log.append(f"Step: removed {worst[1]} (p={worst[0]:.4f})")
            else:
                break
        if not changed:
            break
    return selected, log


Xtr, ytr = design(pd_train), pd_train["default_flag"]
selected, steps = stepwise_logit(Xtr, ytr, EFFECTS)
print("\n".join(steps))
model_cols = [c for e in selected for c in EFFECTS[e]]
print("Selected effects:", selected)
if not model_cols:
    raise SystemExit("No effect entered the model - relax SLENTRY or add data.")
pd_model = fit_logit(Xtr, ytr, model_cols)
print(pd_model.summary())

pd_params = pd.DataFrame({"parameter": pd_model.params.index, "estimate": pd_model.params.values,
                          "std_error": pd_model.bse.values, "p_value": pd_model.pvalues.values,
                          "odds_ratio": np.exp(pd_model.params.values)})
with open(MODEL_DIR / "pd_model.pkl", "wb") as fh:           # = SAS OUTMODEL
    pickle.dump({"result": pd_model, "columns": model_cols}, fh)


def pd_score(df):
    X = sm.add_constant(design(df)[model_cols].astype(float), has_constant="add")
    return pd_model.predict(X)


# 4.3 discrimination metrics: AUC, Gini, KS
def pd_metrics(df, tag):
    y, p = df["default_flag"], pd_score(df)
    fpr, tpr, _ = roc_curve(y, p)
    auc = roc_auc_score(y, p)
    return {"sample": tag, "auc": auc, "gini": 2 * auc - 1, "ks": float(np.max(tpr - fpr))}


pd_performance = pd.DataFrame([pd_metrics(pd_train, "train"), pd_metrics(pd_valid, "valid")])
print("\nPD model performance (AUC / Gini / KS)\n", pd_performance.round(3).to_string(index=False))

fpr, tpr, _ = roc_curve(pd_valid["default_flag"], pd_score(pd_valid))
plt.figure(figsize=(5, 5)); plt.plot(fpr, tpr, label="validation"); plt.plot([0, 1], [0, 1], "k--")
plt.xlabel("1 - specificity"); plt.ylabel("sensitivity"); plt.title("PD model ROC (validation)")
plt.legend(); plt.tight_layout(); plt.savefig(OUT_DIR / "pd_roc_curve.png", dpi=120); plt.close()

# 4.4 score full portfolio -> 12-month PD
pd_scored = pd_features.copy()
pd_scored["pd_12m"] = pd_score(pd_scored)

# 4.5 calibration by risk quintile (group 0 = riskiest, as in SAS PROC RANK DESCENDING)
pd_scored["risk_group"] = 4 - pd.qcut(pd_scored["pd_12m"].rank(method="first"), 5, labels=False)
pd_calibration = (pd_scored.groupby("risk_group")
                  .agg(n_loans=("pd_12m", "size"), avg_pd=("pd_12m", "mean"),
                       observed_dr=("default_flag", "mean")).reset_index())
print("\nPD calibration (group 0 = riskiest)\n", pd_calibration.round(4).to_string(index=False))

# ----------------------------------------------------------------------
# 5. LGD MODEL - OLS on recovery rate
# ----------------------------------------------------------------------
banner("5. LGD MODEL")
lg = stg_lgd.copy()


def lgd_features(df, ead_col):
    out = df.copy()
    out["coverage_ratio"] = np.where(out["secured_flag"] == 1,
                                     np.minimum(out["collateral_value"] / out[ead_col], 1.5), 0.0)
    out["log_ead"] = np.log(np.maximum(out[ead_col], 1))
    out["is_card"] = (out["product_type"] == "Credit Card").astype(float)
    out["is_overdraft"] = (out["product_type"] == "Overdraft").astype(float)
    out["is_revolving"] = (out["product_type"] == "Revolving Credit").astype(float)
    return out


lg["recovery_rate"] = (lg["recovery_amount"] / lg["exposure_at_default"]).clip(0, 1)
lg["lgd_observed"] = 1 - lg["recovery_rate"]
lgd_features_df = lgd_features(lg, "exposure_at_default")
print(lgd_features_df[["recovery_rate", "lgd_observed"]].describe().loc[["count", "mean", "min", "max"]].T)

LGD_X = ["secured_flag", "coverage_ratio", "is_card", "is_overdraft", "is_revolving", "log_ead"]
Xl = sm.add_constant(lgd_features_df[LGD_X].astype(float))
lgd_model = sm.OLS(lgd_features_df["recovery_rate"], Xl).fit()
print(lgd_model.summary())

vif = [variance_inflation_factor(Xl.values, i) for i in range(1, Xl.shape[1])]
std_beta = [lgd_model.params[c] * lgd_features_df[c].std() / lgd_features_df["recovery_rate"].std() for c in LGD_X]
lgd_params = pd.DataFrame({"parameter": ["const"] + LGD_X, "estimate": lgd_model.params.values,
                           "std_error": lgd_model.bse.values, "p_value": lgd_model.pvalues.values,
                           "vif": [np.nan] + vif, "std_beta": [np.nan] + std_beta})
print("\nLGD parameters (VIF and standardised beta)\n", lgd_params.round(4).to_string(index=False))
print(f"R-squared: {lgd_model.rsquared:.4f}   Adj R-squared: {lgd_model.rsquared_adj:.4f}   "
      f"RMSE: {np.sqrt(lgd_model.mse_resid):.4f}")
with open(MODEL_DIR / "lgd_model.pkl", "wb") as fh:
    pickle.dump({"result": lgd_model, "columns": LGD_X}, fh)

# ----------------------------------------------------------------------
# 6. EAD - CCF rules and portfolio segmentation
# ----------------------------------------------------------------------
banner("6. EAD / CCF")
ef = stg_ead[stg_ead["credit_limit"] > stg_ead["drawn_12m_before_default"]].copy()
ef["undrawn_t12"] = ef["credit_limit"] - ef["drawn_12m_before_default"]
ef["ccf_obs"] = ((ef["exposure_at_default"] - ef["drawn_12m_before_default"]) / ef["undrawn_t12"]).clip(0, 1)
ef["util_t12"] = ef["drawn_12m_before_default"] / ef["credit_limit"]
ef["util_band"] = np.where(ef["util_t12"] < 0.5, "LOW", "HIGH")

ccf_seg = (ef.groupby(["product_type", "util_band"])["ccf_obs"]
           .agg(n_obs="size", ccf_seg="mean").reset_index())
ccf_prod = ef.groupby("product_type")["ccf_obs"].agg(n_prod="size", ccf_prod="mean").reset_index()
print("Credit Conversion Factors by segment\n", ccf_seg.round(3).to_string(index=False))

pf = (pd_scored.merge(ccf_seg, on=["product_type", "util_band"], how="left")
      .merge(ccf_prod, on="product_type", how="left"))
pf["ccf"] = np.where(pf["product_type"] == "Term Loan", 0.0,
                     np.where(pf["n_obs"] >= MIN_SEG_N, pf["ccf_seg"], pf["ccf_prod"].fillna(1.0)))
pf["ead"] = pf["outstanding_balance"] + pf["ccf"] * pf["undrawn_amount"]

# ----------------------------------------------------------------------
# 7. SCORE LGD ON THE PORTFOLIO (uses final EAD)
# ----------------------------------------------------------------------
banner("7. LGD SCORING")
pf = lgd_features(pf, "ead")
pf["pred_rr"] = lgd_model.predict(sm.add_constant(pf[LGD_X].astype(float), has_constant="add"))

# ----------------------------------------------------------------------
# 8. IFRS 9 STAGING AND ECL      ECL = PD x LGD x EAD x discount factor
# ----------------------------------------------------------------------
banner("8. IFRS 9 STAGING AND ECL")
e = pf.copy()
e["pred_rr"] = e["pred_rr"].clip(0, 1)
e["lgd"] = np.maximum(1 - e["pred_rr"], LGD_FLOOR)

e["stage"] = np.select([e["current_dpd"] >= 90,
                        (e["current_dpd"] >= 30) | (e["pd_12m"] >= SICR_PD)], [3, 2], default=1)
e["pd_lifetime"] = 1 - (1 - e["pd_12m"]) ** np.maximum(e["remaining_term_years"], 1)
e["pd_used"] = np.select([e["stage"] == 1, e["stage"] == 2], [e["pd_12m"], e["pd_lifetime"]], default=1.0)
e["t_disc"] = np.select([e["stage"] == 1, e["stage"] == 2],
                        [0.5, np.maximum(e["remaining_term_years"], 1) / 2], default=0.0)
e["disc_factor"] = 1 / (1 + e["eir"]) ** e["t_disc"]
e["ecl"] = e["pd_used"] * e["lgd"] * e["ead"] * e["disc_factor"]
e["ecl_12m"] = e["pd_12m"] * e["lgd"] * e["ead"] / (1 + e["eir"]) ** 0.5

keep = ["loan_id", "customer_id", "product_type", "util_band", "stage", "current_dpd",
        "outstanding_balance", "undrawn_amount", "ccf", "ead", "pd_12m", "pd_lifetime", "pd_used",
        "lgd", "eir", "disc_factor", "ecl", "ecl_12m", "default_flag"]
ecl_loan_level = e[keep].sort_values("ecl", ascending=False).reset_index(drop=True)

# ----------------------------------------------------------------------
# 9. REPORTING AND EXPORT
# ----------------------------------------------------------------------
banner("9. REPORTING")


def summarise(df, by, extra=False):
    g = df.groupby(by)
    out = g.agg(n_loans=("loan_id", "size"), total_ead=("ead", "sum"), total_ecl=("ecl", "sum"))
    if extra:
        out["avg_pd"] = g["pd_12m"].mean(); out["avg_lgd"] = g["lgd"].mean()
    out["ecl_coverage"] = out["total_ecl"] / out["total_ead"]
    return out.reset_index()


ecl_by_stage = summarise(ecl_loan_level, "stage")
ecl_by_product = summarise(ecl_loan_level, "product_type", extra=True)
fmt = {"total_ead": "{:,.0f}".format, "total_ecl": "{:,.0f}".format, "ecl_coverage": "{:.2%}".format,
       "avg_pd": "{:.2%}".format, "avg_lgd": "{:.2%}".format}
print("IFRS 9 ECL by stage\n", ecl_by_stage.to_string(index=False, formatters=fmt))
print("\nIFRS 9 ECL by product\n", ecl_by_product.to_string(index=False, formatters=fmt))
print("\nTop 10 loans by ECL\n", ecl_loan_level.head(10)[
    ["loan_id", "product_type", "stage", "ead", "pd_12m", "lgd", "ecl"]].to_string(
    index=False, formatters={"ead": "{:,.0f}".format, "ecl": "{:,.0f}".format,
                             "pd_12m": "{:.2%}".format, "lgd": "{:.2%}".format}))
print(f"\nTOTAL EAD: {ecl_loan_level.ead.sum():,.0f}   TOTAL ECL: {ecl_loan_level.ecl.sum():,.0f}")

for name, df in {"ecl_loan_level": ecl_loan_level, "ecl_by_stage": ecl_by_stage,
                 "ecl_by_product": ecl_by_product, "pd_performance": pd_performance,
                 "ccf_segments": ccf_seg, "lgd_parameters": lgd_params,
                 "pd_parameters": pd_params, "pd_calibration": pd_calibration}.items():
    df.to_csv(OUT_DIR / f"{name}.csv", index=False)
print(f"\nCredit risk model run complete. Results in {OUT_DIR}")
