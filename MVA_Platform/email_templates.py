def build_approved_email(lead_data, reason, reviewer_email, accept_url):
    return f"""
    <html>
    <body style="font-family: Arial, sans-serif;">

        <h2 style="color: #28a745;">New Intake has been marked as PROCEED by Reviewer</h2>

        <h3>Lead Details</h3>
        <p><b>Name:</b> {lead_data.get("first_name")} {lead_data.get("last_name")}</p>
        <p><b>Email:</b> {lead_data.get("email")}</p>
        <p><b>Phone:</b> {lead_data.get("phone")}</p>

        <p><b>Case Type:</b> {lead_data.get("case_type")}</p>
        <p><b>Location:</b> {lead_data.get("state_of_incident")}</p>
        <p><b>Date:</b> {lead_data.get("accident_date")}</p>

        <p><b>Case Description:</b><br>{(lead_data.get("case_description") or "").replace("\n", "<br>")}</p>

        <hr>
        <p><b>Reviewed By:</b> {reviewer_email}</p>
        <p><b>Reviewer Note:</b> {reason}</p>

        <br>

        <a href="{accept_url}"
           style="background:#28a745;color:white;padding:14px 22px;text-decoration:none;border-radius:6px;font-size:16px;">
           Accept This Intake
        </a>

    </body>
    </html>
    """

def build_escalated_email(lead_data, reason, reviewer_email):
    return f"""
    <html>
    <body style="font-family: Arial, sans-serif;">

        <h2 style="color: #ffc107;">A lead has been escalated for attorney review.</h2>

        <h3>Lead Information</h3>
        <p><b>Name:</b> {lead_data.get("first_name")} {lead_data.get("last_name")}</p>
        <p><b>Email:</b> {lead_data.get("email")}</p>
        <p><b>Phone:</b> {lead_data.get("phone")}</p>

        <p><b>Case Type:</b> {lead_data.get("case_type")}</p>
        <p><b>Location:</b> {lead_data.get("state_of_incident")}</p>
        <p><b>Date of Incident:</b> {lead_data.get("accident_date")}</p>

        <p><b>Case Description:</b><br>{(lead_data.get("case_description") or "").replace("\n", "<br>")}</p>

        <hr>

        <h3>Escalation Details</h3>
        <p><b>Status:</b> ESCALATED</p>
        <p><b>Escalated By:</b> {reviewer_email}</p>
        <p><b>Reviewer Note:</b> {reason}</p>

        <br>

        <p>
            This matter requires further review.  
            Please assess the case and determine next steps.
        </p>

    </body>
    </html>
    """

def build_declined_email(lead_data):
    first_name = lead_data.get("first_name") or ""

    return f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height:1.6;">

        <p>Dear {first_name},</p>

        <p>
        Thank you for contacting Ramos Law and submitting a request for a free case evaluation.
        </p>

        <p>
        After reviewing the information provided, we are unable to accept this matter for representation.
        </p>

        <p>
        This decision does not mean that you do not have a claim. It only means that Ramos Law is not able
        to represent you in this matter.
        </p>

        <p>
        If you believe you have a valid legal claim, you may wish to contact another attorney or your local
        bar association as soon as possible, as legal deadlines may apply.
        </p>

        <p>We wish you the best.</p>

        <br>

        <p>Sincerely,<br>
        Ramos Law</p>

    </body>
    </html>
    """
def build_declined_with_referrals_email(lead_data, referrals):
    first_name = lead_data.get("first_name") or ""

    referral_list_html = ""

    for r in referrals:
        if not r.referral:
            continue

        ref = r.referral

        referral_list_html += f"""
        <li>
            <b>{ref.organization_name}</b><br>
            Contact Name: {ref.contact_name or "N/A"}<br>
            Email: {ref.email or "N/A"}<br>
            Phone: {ref.phone or "N/A"}<br>
            Practice Area: {ref.practice_area or "N/A"}<br><br>
        </li>
        """

    return f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height:1.6;">

        <p>Dear {first_name},</p>

        <p>
        Thank you for contacting Ramos Law and submitting a request for a free case evaluation.
        </p>

        <p>
        After reviewing the information provided, we are unable to accept this matter for representation.
        </p>

        <p>
        This decision does not mean that you do not have a claim. It only means that Ramos Law is not able
        to represent you in this matter.
        </p>

        <p>
        At this time, Since we are unable to represent you, based on your case,
        we are sharing a few referrals that may be able to assist you:
        </p>

        <ul>
            {referral_list_html}
        </ul>

        <p>
        Please feel free to reach out to them directly. We wish you the best.
        </p>

        <br>

        <p>Sincerely,<br>
        Ramos Law</p>

    </body>
    </html>
    """