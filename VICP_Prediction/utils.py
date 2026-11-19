import joblib
import pandas as pd

settlement_model = joblib.load("VICP_Prediction/Models/settlement_prediction_model.pkl")
award_range = joblib.load("VICP_Prediction/Models/award_prediction_model.pkl")
award_model = award_range["model"]
mae_award_model = award_range["mae"]
    
async def estimate_vicp_fees(case_outcome, injury, procedure_present):
    # Injury-based baseline hours
    injury_baseline = {
        "SIRVA": 200,
        "GBS": 135,
        "STROKE": 150,
        "Neuropathy": 120,
        "Other": 100
    }
    
    base_hours = injury_baseline.get(injury, 100)
    
    # Procedure adjustment
    if procedure_present:
        base_hours += 20

    # Outcome adjustment
    if case_outcome == "compensated":
        base_hours += 15
    else:
        base_hours += 5

    hourly_rate = 350
    estimated_fees = base_hours * hourly_rate

    return base_hours, estimated_fees


async def predict_case(case_data):
    df = pd.DataFrame([case_data])

    y_pred = settlement_model.predict(df)
    y_prob = settlement_model.predict_proba(df)

    prob_comp = y_prob[0][1] * 100
    outcome = "compensated" if y_pred[0] == 1 else "dismissed"

    result = {
        "probability_of_compensation (%)": round(prob_comp, 2),
        "predicted_outcome": outcome.title(),
    }

    if outcome == "compensated":
        predicted_award = float(award_model.predict(df)[0])
        lower = max(0, predicted_award - mae_award_model)
        upper = predicted_award + mae_award_model

        result["expected_award_range"] = f"${lower:,.0f} – ${upper:,.0f}"
        result["predicted_award (point estimate)"] = f"${predicted_award:,.0f}"

    hours, fees = await estimate_vicp_fees(
        outcome,
        case_data["injury"],
        # case_data["text_length"],
        case_data["procedure_present"]
    )

    result["estimated_attorney_hours"] = hours
    result["estimated_attorney_fees"] = f"${fees:,.0f}"

    return result