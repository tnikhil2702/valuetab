from collections import defaultdict

PHRASE_OVERRIDES = {
    "amended petition": ("drafting", 4),
    "draft petition": ("drafting", 4),
    "petition filing": ("court_filing", 3),
    "rule 4 report": ("respondent_report", 5),
    "respondent report": ("respondent_report", 5),
    "medical chronology": ("medical_records", 3)
}

CATEGORY_KEYWORDS = {
"medical_records": {
"medical": 3,
"records": 3,
"treatment": 1,
"request records": 3,
"record requests":3,
"requesting records":3,
"obtain records": 4,
"medical bills": 2,
"provider": 2,
"clinic": 2,
"HIPPA": 4,
"upload file":3,
"save file":3,
"review file":2
},

"court_filing": {
"filing docket check": 3,
"filing": 3,
"e-file": 3,
"court filing": 3,
"docket": 2,
"electronic filing": 3,
"submit filing": 2,
},

"exhibits": {
"exhibit": 3,
"exhibits": 3,
"organize": 2,
"compile": 2,
"index": 2,
"label": 1,
"prepare exhibits": 3
},

"case_management": {
"legal team communication":3,
"Correspondence with team":3,
"review case":3,
"correspondence":3,
"phone call":3,
"case update":3,
"update": 3,
"status": 3,
"calendar": 3,
"schedule": 2,
"follow up": 3,
"follow-up": 2,
"coordinate": 2,
"email": 4,
"receipt":3,
"apologies":2
},

"drafting": {
"drafting petition":4,
"drafting":4,
"petition": 3,
"draft": 3,
"revise": 3,
"prepare petition": 4,
"amended petition": 5,
},

"case_strategy": {
"strategy": 4,
"analysis": 3,
"legal research": 3
},

"respondent_report": {
"respondent report": 5,
"rule 4 report": 5,
"rule4": 5,
"government report": 4,
"doj report": 4,
"rr report": 5
},

"intake_review": {
"intake": 5,
"analyze intake": 5,
"intake review": 5,
"new case intake": 3,
"case intake": 5,
"initial case review": 3,
"initial petition":3
},

"client_communication": {
    "client communication":3,
    "phone call":3
},

"medical_records_review":{
    "review records":5
}
}

ENTITY_KEYWORDS = {

    "client": ["client", "cl", "petitioner"],

    "provider": [
        "doctor", "physician", "hospital", "clinic",
        "provider", "medical provider", "treating physician"
    ],

    "mro": [
        "medical records office", "mro", "records office"
    ],

    "court": [
        "court", "docket", "ecf", "cmecf"
    ],

    "court_staff": [
        "clerk", "court clerk", "judge", "special master"
    ],

    "respondent": [
        "respondent", "department of justice",
        "doj", "government", "gov"
    ],

    "expert": [
        "expert", "consultant", "reviewing physician",
        "medical expert"
    ],

    "paralegal": [
        "paralegal", "assistant"
    ]
}

ACTION_KEYWORDS = {

    "phone_call": [
        "phone", "call", "telephone", "voicemail",
        "left message", "on hold", "phone call"
    ],

    "conference": [
        "conference", "meeting", "discussion", "call with"
    ],

    "review": [
        "review", "analyze", "analysis",
        "examine", "assess", "evaluate", "review records"
    ],

    "draft": [
        "draft", "revise", "edit",
        "prepare", "redraft"
    ],

    "organize": [
        "organize", "compile", "assemble",
        "categorize", "index"
    ],

    "request": [
        "request", "obtain", "follow up",
        "follow-up", "send request"
    ],

    "file": [
        "file", "efile", "e-file",
        "submit", "upload", "docket", "review file", "upload file", "save file"
    ],

    "schedule": [
        "schedule", "calendar", "set up",
        "arrange"
    ]
}

async def detect_action(text):
    for action, keywords in ACTION_KEYWORDS.items():
        for word in keywords:
            if word in text:
                return action
    return None

async def detect_entity(text):
    for entity, keywords in ENTITY_KEYWORDS.items():
        for word in keywords:
            if word in text:
                return entity
    return None

# Main classification function
async def classify_entry(text):
    entity = await detect_entity(text)
    action = await detect_action(text)
    scores = defaultdict(int)
    for category, keywords in CATEGORY_KEYWORDS.items():
        score = 0
        for word, weight in keywords.items():
            if word in text:
                score += weight
        scores[category] = score
    
    for phrase, (category, weight) in PHRASE_OVERRIDES.items():
        if phrase in text:
            scores[category] += weight

    # context overrides
    if action == "phone_call":
        if entity == "client":
            scores["client_communication"] += 5
            scores["case_management"] += 3
            scores["case_strategy"] += 2

        elif entity in ["provider", "mro"]:
            scores["medical_records"] += 4

        elif entity == "expert":
            scores["case_strategy"] += 3

        elif entity == "respondent":
            scores["respondent_report"] += 4

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    primary_category, primary_score = ranked[0]
    secondary_category = ranked[1][0]
    secondary_score = ranked[1][1]

    total_score = sum(scores.values())

    if total_score == 0:
        confidence = 0
    else:
        confidence = primary_score / total_score
    mixed = abs(primary_score - secondary_score) <= 1

    compound_signals = 0
    for cat, score in scores.items():
        if score > 0:
            compound_signals += 1

    if compound_signals >= 3:
        mixed = True 

    return {
        "primary_category": primary_category,
        "secondary_category": secondary_category,
        "confidence": round(confidence, 2),
        "mixed": mixed,
        "entity": entity,
        "action": action
    }