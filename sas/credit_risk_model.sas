/*=====================================================================
  PROJECT : SAS Credit Risk Model Development (IFRS 9)
  MODELS  : PD (PROC LOGISTIC), LGD (PROC REG), EAD (CCF segmentation)
  OUTPUT  : Loan-level and portfolio-level Expected Credit Loss (ECL)

  HOW TO RUN
  1. Copy the folder "credit_risk_project" to your machine / SAS server.
  2. Edit ONLY the %LET proj_path line in section 0 below.
  3. Open this file in SAS Studio / SAS Enterprise Guide / Base SAS and
     submit the whole program (F3 / Run).  Review the log for ERRORs.

  FOLDER LAYOUT (created/used by the program)
     <proj_path>/sas/credit_risk_model.sas
     <proj_path>/data/pd_loan_data.csv     (input, 100 rows)
     <proj_path>/data/lgd_data.csv         (input, 100 rows)
     <proj_path>/data/ead_data.csv         (input, 100 rows)
     <proj_path>/sasdata/                  (permanent SAS datasets/model)
     <proj_path>/output/                   (CSV result files)
=====================================================================*/

/*---------------------------------------------------------------
  0. SETUP  -  global parameters
---------------------------------------------------------------*/
/* Windows example : C:/credit_risk_project
   SAS OnDemand    : /home/u12345678/credit_risk_project          */
%let proj_path = C:/credit-risk-ifrs9-sas-python;   /* <<< CHANGE THIS: repository ROOT folder (contains data/ and sas/), not the .sas file */

/* NOTE: the former USE_DB switch was removed. The program now always reads the
   CSV files. See the commented Oracle/Teradata template in Section 1.        */
%let seed        = 2026;   /* reproducible train/validation split               */
%let lgd_floor   = 0.05;   /* minimum LGD (5%)                                  */
%let sicr_pd     = 0.30;   /* 12m PD at/above this -> Stage 2 (SICR trigger)    */
%let min_seg_n   = 5;      /* min obs for a CCF segment to be used              */

/* Database credentials - COMMENTED OUT because the demo does not connect to a
   database. Un-comment them only if you activate the template in Section 1.
%let ora_user = your_user;   %let ora_pwd = your_password;  %let ora_path = ORCL;
%let td_user  = your_user;   %let td_pwd  = your_password;  %let td_server = tdprod;
*/

options mprint nosymbolgen validvarname=v7 nodate nonumber;
ods graphics on;

/* create working sub-folders if they do not exist */
%let rc = %sysfunc(dcreate(sasdata,&proj_path));
%let rc = %sysfunc(dcreate(output,&proj_path));
libname crlib "&proj_path/sasdata";

/*---------------------------------------------------------------
  1. DATA EXTRACTION
---------------------------------------------------------------*/

/*=====================================================================
  ORACLE / TERADATA CONNECTION CODE  -  COMMENTED OUT ON PURPOSE

  WHY IT IS COMMENTED OUT
  - It needs a live Oracle and Teradata data warehouse, valid user
    credentials, network access, and a SAS/ACCESS licence. A demo or a
    SAS OnDemand for Academics account has none of these, so running the
    code would fail with connection errors and stop the whole program.
  - The table and column names below (loan_master, loan_performance,
    dw.defaulted_loans_workout, ...) are PLACEHOLDERS. They must be
    replaced with the real tables of your own data warehouse.
  - Passwords should never be stored in a program file. Use a secure
    credential store or SAS authentication domains in production.

  HOW WE USE AN ALTERNATIVE (the active code below)
  - The three 100-row CSV files in <proj_path>/data stand in for the
    database tables:
        pd_loan_data.csv  ->  Oracle  : loan + customer + performance tables
        lgd_data.csv      ->  Teradata: defaulted-loan workout/recoveries
        ead_data.csv      ->  Teradata: defaulted-facility balance history
  - They are read with DATA step INFILE and create the SAME WORK tables
    (raw_pd, raw_lgd, raw_ead) with the SAME column names that the
    database queries would create. Every later step (cleaning, features,
    PD, LGD, EAD, ECL) therefore works identically with either source.

  HOW TO SWITCH TO THE DATABASE LATER
  1. Un-comment the credentials in Section 0 and the block below.
  2. Edit the SQL so table/column names match your warehouse; keep the
     output column names identical to the CSV headers.
  3. Comment out (or delete) the "ACTIVE" CSV block further below.
  4. Make sure SAS/ACCESS to Oracle and to Teradata is licensed.

  ---- Oracle: loan performance (creates raw_pd) ----
  proc sql;
    connect to oracle (user="&ora_user" password="&ora_pwd" path="&ora_path");
    create table raw_pd as
    select * from connection to oracle (
      select l.loan_id, l.customer_id, l.product_type, c.age, c.gender,
             c.marital_status, c.employment_years, c.annual_income,
             l.credit_limit, l.outstanding_balance, l.monthly_debt_payment,
             l.open_date, l.snapshot_date, p.num_delinq_30_12m,
             p.num_delinq_60_12m, p.num_delinq_90_12m, p.current_dpd,
             l.remaining_term_years, l.eir, l.secured_flag,
             l.collateral_value, p.default_flag
      from   loan_master l
      join   customer c on c.customer_id = l.customer_id
      join   loan_performance p on p.loan_id = l.loan_id
      where  p.snapshot_date = date '2026-09-30' );
    disconnect from oracle;
  quit;

  ---- Teradata: recoveries and facility history (creates raw_lgd, raw_ead) ----
  proc sql;
    connect to teradata (user="&td_user" password="&td_pwd" server="&td_server");
    create table raw_lgd as
    select * from connection to teradata (
      select loan_id, product_type, secured_flag, collateral_type,
             collateral_value, exposure_at_default, default_date,
             recovery_months, recovery_amount
      from   dw.defaulted_loans_workout );
    create table raw_ead as
    select * from connection to teradata (
      select facility_id, product_type, credit_limit,
             drawn_12m_before_default, exposure_at_default, default_date
      from   dw.defaulted_facilities );
    disconnect from teradata;
  quit;
=====================================================================*/

/*---------------------------------------------------------------
  ACTIVE CODE: read the demo CSV files (alternative to the database)
---------------------------------------------------------------*/
%macro extract_data;

  data raw_pd;
    infile "&proj_path/data/pd_loan_data.csv" dsd firstobs=2 truncover;
    length loan_id $10 customer_id $10 product_type $20 gender $1
           marital_status $10;
    input loan_id $ customer_id $ product_type $ age gender $ marital_status $
          employment_years annual_income credit_limit outstanding_balance
          monthly_debt_payment open_date :yymmdd10. snapshot_date :yymmdd10.
          num_delinq_30_12m num_delinq_60_12m num_delinq_90_12m current_dpd
          remaining_term_years eir secured_flag collateral_value default_flag;
    format open_date snapshot_date date9.;
  run;

  data raw_lgd;
    infile "&proj_path/data/lgd_data.csv" dsd firstobs=2 truncover;
    length loan_id $10 product_type $20 collateral_type $10;
    input loan_id $ product_type $ secured_flag collateral_type $
          collateral_value exposure_at_default default_date :yymmdd10.
          recovery_months recovery_amount;
    format default_date date9.;
  run;

  data raw_ead;
    infile "&proj_path/data/ead_data.csv" dsd firstobs=2 truncover;
    length facility_id $10 product_type $20;
    input facility_id $ product_type $ credit_limit drawn_12m_before_default
          exposure_at_default default_date :yymmdd10.;
    format default_date date9.;
  run;

%mend extract_data;
%extract_data;

/* quick data-quality profile */
proc means data=raw_pd n nmiss mean min max;
  title "Data quality profile - PD / portfolio data";
  var age employment_years annual_income credit_limit outstanding_balance
      monthly_debt_payment current_dpd default_flag;
run;

/*---------------------------------------------------------------
  2. DATA CLEANING  (PROC SQL filters, de-duplication, imputation)
---------------------------------------------------------------*/
proc sql;
  create table stg_pd as
  select distinct *
  from raw_pd
  where loan_id is not missing
    and credit_limit > 0
    and outstanding_balance >= 0
    and age between 18 and 80
    and default_flag in (0,1);

  create table stg_lgd as
  select distinct *
  from raw_lgd
  where loan_id is not missing and exposure_at_default > 0
    and recovery_amount >= 0;

  create table stg_ead as
  select distinct *
  from raw_ead
  where facility_id is not missing and credit_limit > 0;

  select count(*) as n_pd  from stg_pd;
  select count(*) as n_lgd from stg_lgd;
  select count(*) as n_ead from stg_ead;
quit;

/* flag + median-impute missing values */
data stg_pd;
  set stg_pd;
  miss_income_flag = missing(annual_income);
  miss_emp_flag    = missing(employment_years);
run;

proc stdize data=stg_pd out=pd_clean reponly method=median;
  var annual_income employment_years;
run;

/*---------------------------------------------------------------
  3. FEATURE ENGINEERING (borrower level)
---------------------------------------------------------------*/
data pd_features;
  length util_band $4;
  set pd_clean;

  utilization        = min(outstanding_balance / credit_limit, 1.5);  /* credit utilisation   */
  dti                = min((monthly_debt_payment * 12) / annual_income, 1.0); /* debt-to-income */
  account_age_months = intck('month', open_date, snapshot_date);      /* account age          */
  total_delinq_12m   = num_delinq_30_12m + num_delinq_60_12m + num_delinq_90_12m;
  delinq_severity    = num_delinq_30_12m + 2*num_delinq_60_12m + 3*num_delinq_90_12m;
  ever_delinq_flag   = (total_delinq_12m > 0);
  log_income         = log(annual_income);
  undrawn_amount     = max(credit_limit - outstanding_balance, 0);

  if secured_flag = 1 and outstanding_balance > 0 then
       coverage_ratio = min(collateral_value / outstanding_balance, 1.5);
  else coverage_ratio = 0;

  if utilization < 0.5 then util_band = 'LOW'; else util_band = 'HIGH';
run;

proc means data=pd_features n mean std min max;
  title "Engineered features";
  var utilization dti account_age_months total_delinq_12m delinq_severity;
run;

/*---------------------------------------------------------------
  4. PD MODEL  -  PROC LOGISTIC
---------------------------------------------------------------*/
/* 4.1 stratified 70/30 train / validation split */
proc sort data=pd_features out=pd_sorted; by default_flag; run;

proc surveyselect data=pd_sorted out=pd_split samprate=0.70 seed=&seed
                  outall method=srs;
  strata default_flag;
run;

data pd_train pd_valid;
  set pd_split;
  if selected = 1 then output pd_train;
  else output pd_valid;
run;

/* 4.2 fit the model (stepwise selection) and store it */
proc logistic data=pd_train outmodel=crlib.pd_model plots(only)=roc;
  title "PD model - logistic regression (training sample)";
  class marital_status (param=ref ref='Single');
  model default_flag(event='1') =
        utilization dti account_age_months delinq_severity
        age employment_years log_income marital_status
        / selection=stepwise slentry=0.30 slstay=0.20 lackfit;
  ods output ParameterEstimates=pd_params;
run;

/* 4.3 discrimination metrics: AUC, Gini, KS */
%macro pd_metrics(data=, tag=);
  proc logistic inmodel=crlib.pd_model;
    score data=&data out=&tag._scored outroc=&tag._roc;
  run;

  proc sort data=&tag._roc; by _1mspec_ _sensit_; run;

  data &tag._perf;
    length sample $10;
    set &tag._roc end=eof;
    retain prev_x 0 prev_y 0 auc 0 ks 0;
    auc + (_1mspec_ - prev_x) * (_sensit_ + prev_y) / 2;   /* trapezoid rule */
    ks = max(ks, abs(_sensit_ - _1mspec_));
    prev_x = _1mspec_;  prev_y = _sensit_;
    if eof then do;
      auc + (1 - prev_x) * (1 + prev_y) / 2;
      gini = 2*auc - 1;
      sample = "&tag";
      output;
    end;
    keep sample auc gini ks;
  run;
%mend pd_metrics;

%pd_metrics(data=pd_train, tag=train);
%pd_metrics(data=pd_valid, tag=valid);

data pd_performance; set train_perf valid_perf; run;
proc print data=pd_performance noobs;
  title "PD model performance (AUC / Gini / KS)";
  format auc gini ks 6.3;
run;

/* 4.4 score the full portfolio -> 12-month PD */
proc logistic inmodel=crlib.pd_model;
  score data=pd_features out=pd_scored;
run;

data pd_scored;
  set pd_scored;
  pd_12m = p_1;
run;

/* 4.5 calibration check by risk quintile */
proc rank data=pd_scored out=pd_ranked groups=5 descending;
  var pd_12m;
  ranks risk_group;
run;

proc means data=pd_ranked noprint nway;
  class risk_group;
  var pd_12m default_flag;
  output out=pd_calibration(drop=_type_ _freq_)
         n(pd_12m)=n_loans mean(pd_12m)=avg_pd mean(default_flag)=observed_dr;
run;

proc print data=pd_calibration noobs;
  title "PD calibration: predicted vs observed default rate (group 0 = riskiest)";
  format avg_pd observed_dr percent8.2;
run;

/*---------------------------------------------------------------
  5. LGD MODEL  -  PROC REG on recovery rates
---------------------------------------------------------------*/
data lgd_features;
  set stg_lgd;
  recovery_rate = min(max(recovery_amount / exposure_at_default, 0), 1);
  lgd_observed  = 1 - recovery_rate;
  if secured_flag = 1 then coverage_ratio = min(collateral_value / exposure_at_default, 1.5);
  else coverage_ratio = 0;
  log_ead       = log(exposure_at_default);
  is_card       = (product_type = 'Credit Card');
  is_overdraft  = (product_type = 'Overdraft');
  is_revolving  = (product_type = 'Revolving Credit');
run;

proc means data=lgd_features n mean min max;
  title "Observed recovery rate and LGD";
  var recovery_rate lgd_observed;
run;

proc reg data=lgd_features plots=none;
  title "LGD model - OLS regression on recovery rate";
  lgd_reg: model recovery_rate = secured_flag coverage_ratio is_card
                                 is_overdraft is_revolving log_ead / vif stb;
  output out=lgd_fit p=pred_rr r=resid;
  ods output FitStatistics=lgd_fitstats ParameterEstimates=lgd_params;
run;
quit;

/*---------------------------------------------------------------
  6. EAD  -  CCF rules and portfolio segmentation
---------------------------------------------------------------*/
data ead_features;
  length util_band $4;
  set stg_ead;
  where credit_limit > drawn_12m_before_default;
  undrawn_t12 = credit_limit - drawn_12m_before_default;
  ccf_obs     = (exposure_at_default - drawn_12m_before_default) / undrawn_t12;
  ccf_obs     = min(max(ccf_obs, 0), 1);               /* bound CCF to [0,1] */
  util_t12    = drawn_12m_before_default / credit_limit;
  if util_t12 < 0.5 then util_band = 'LOW'; else util_band = 'HIGH';
run;

/* CCF by product x utilisation band, and by product (fallback) */
proc means data=ead_features noprint nway;
  class product_type util_band;
  var ccf_obs;
  output out=ccf_seg(drop=_type_ _freq_) n=n_obs mean=ccf_seg;
run;

proc means data=ead_features noprint nway;
  class product_type;
  var ccf_obs;
  output out=ccf_prod(drop=_type_ _freq_) n=n_prod mean=ccf_prod;
run;

proc print data=ccf_seg noobs;
  title "Credit Conversion Factors by segment";
  format ccf_seg 6.3;
run;

/* apply rules: Term Loan CCF = 0 ; revolving products use segment CCF,
   falling back to product CCF when the segment has too few observations */
proc sql;
  create table pf_ead as
  select a.*,
         case when a.product_type = 'Term Loan'     then 0
              when s.n_obs >= &min_seg_n            then s.ccf_seg
              else coalesce(p.ccf_prod, 1) end       as ccf,
         a.outstanding_balance
           + calculated ccf * a.undrawn_amount       as ead
  from pd_scored a
       left join ccf_seg  s on a.product_type = s.product_type
                           and a.util_band    = s.util_band
       left join ccf_prod p on a.product_type = p.product_type;
quit;

/*---------------------------------------------------------------
  7. SCORE LGD ON THE PORTFOLIO (uses final EAD)
     Portfolio rows are appended with a missing response; PROC REG
     predicts them without using them to estimate the model.
---------------------------------------------------------------*/
data lgd_scoring_input;
  set lgd_features(in=a) pf_ead(in=b);
  if b then do;
    recovery_rate = .;
    if secured_flag = 1 and ead > 0 then coverage_ratio = min(collateral_value / ead, 1.5);
    else coverage_ratio = 0;
    log_ead      = log(max(ead, 1));
    is_card      = (product_type = 'Credit Card');
    is_overdraft = (product_type = 'Overdraft');
    is_revolving = (product_type = 'Revolving Credit');
  end;
  length src $10;
  src = ifc(b, 'PORTFOLIO', 'TRAIN');
run;

proc reg data=lgd_scoring_input noprint;
  model recovery_rate = secured_flag coverage_ratio is_card
                        is_overdraft is_revolving log_ead;
  output out=lgd_scored(where=(src='PORTFOLIO')) p=pred_rr;
run;
quit;

/*---------------------------------------------------------------
  8. IFRS 9 STAGING AND EXPECTED CREDIT LOSS
        ECL = PD x LGD x EAD x discount factor
---------------------------------------------------------------*/
data ecl_loan_level;
  set lgd_scored;

  /* LGD from predicted recovery rate (bounded, with floor) */
  pred_rr = min(max(pred_rr, 0), 1);
  lgd     = max(1 - pred_rr, &lgd_floor);

  /* IFRS 9 staging */
  if      current_dpd >= 90                          then stage = 3;
  else if current_dpd >= 30 or pd_12m >= &sicr_pd    then stage = 2;
  else                                                    stage = 1;

  /* PD horizon: 12m for Stage 1, lifetime for Stage 2, 100% for Stage 3 */
  pd_lifetime = 1 - (1 - pd_12m) ** max(remaining_term_years, 1);
  if      stage = 1 then do; pd_used = pd_12m;      t_disc = 0.5;                        end;
  else if stage = 2 then do; pd_used = pd_lifetime; t_disc = max(remaining_term_years,1)/2; end;
  else                   do; pd_used = 1;           t_disc = 0;                          end;

  disc_factor = 1 / ((1 + eir) ** t_disc);
  ecl         = pd_used * lgd * ead * disc_factor;
  ecl_12m     = pd_12m  * lgd * ead * (1 / ((1 + eir) ** 0.5));

  format pd_12m pd_lifetime pd_used lgd ccf percent8.2
         ead ecl ecl_12m comma15.;
  keep loan_id customer_id product_type util_band stage current_dpd
       outstanding_balance undrawn_amount ccf ead pd_12m pd_lifetime pd_used
       lgd eir disc_factor ecl ecl_12m default_flag;
run;

/*---------------------------------------------------------------
  9. REPORTING AND EXPORT
---------------------------------------------------------------*/
proc sql;
  create table ecl_by_stage as
  select stage,
         count(*)        as n_loans,
         sum(ead)        as total_ead format=comma18.,
         sum(ecl)        as total_ecl format=comma18.,
         calculated total_ecl / calculated total_ead as ecl_coverage format=percent8.2
  from ecl_loan_level
  group by stage;

  create table ecl_by_product as
  select product_type,
         count(*)        as n_loans,
         sum(ead)        as total_ead format=comma18.,
         avg(pd_12m)     as avg_pd    format=percent8.2,
         avg(lgd)        as avg_lgd   format=percent8.2,
         sum(ecl)        as total_ecl format=comma18.,
         calculated total_ecl / calculated total_ead as ecl_coverage format=percent8.2
  from ecl_loan_level
  group by product_type;
quit;

proc print data=ecl_by_stage   noobs; title "IFRS 9 ECL by stage";   run;
proc print data=ecl_by_product noobs; title "IFRS 9 ECL by product"; run;

proc sort data=ecl_loan_level; by descending ecl; run;
proc print data=ecl_loan_level(obs=10) noobs;
  title "Top 10 loans by ECL";
run;

/* permanent copies */
data crlib.ecl_loan_level;   set ecl_loan_level;   run;
data crlib.ccf_segments;     set ccf_seg;          run;

/* CSV exports */
proc export data=ecl_loan_level  outfile="&proj_path/output/ecl_loan_level.csv"
            dbms=csv replace; run;
proc export data=ecl_by_stage    outfile="&proj_path/output/ecl_by_stage.csv"
            dbms=csv replace; run;
proc export data=ecl_by_product  outfile="&proj_path/output/ecl_by_product.csv"
            dbms=csv replace; run;
proc export data=pd_performance  outfile="&proj_path/output/pd_performance.csv"
            dbms=csv replace; run;
proc export data=ccf_seg         outfile="&proj_path/output/ccf_segments.csv"
            dbms=csv replace; run;
proc export data=lgd_params      outfile="&proj_path/output/lgd_parameters.csv"
            dbms=csv replace; run;
proc export data=pd_params       outfile="&proj_path/output/pd_parameters.csv"
            dbms=csv replace; run;

title; ods graphics off;
%put NOTE: Credit risk model run complete. Results in &proj_path/output;
