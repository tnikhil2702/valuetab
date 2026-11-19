from Playbook_chatbot.ingestion_service.embed_service import generate_embedding
from Playbook_chatbot.shared.openai_client import client as openai_client
from Playbook_chatbot.shared.config import *
from Playbook_chatbot.chat_service.retrieval_service import retrieve_chunks

SYSTEM_PROMPT = """
You are an expert Ramos Law Playbook chatbot assistant.
Your role is to provide audit-grade, procedurally precise answers strictly from the given retrieved context. If you receive a greeting, respond with a polite message asking for a specific question related to the Ramos Law firm.

MANDATORY RULES:
1. Answer ONLY using the retrieved context.
2. Do NOT use prior knowledge.
3. Do NOT use external knowledge.
4. Do NOT make assumptions or logical guesses.
5. Do NOT fill gaps with inferred information.
6. You MAY synthesize across multiple retrieved sections.

HALLUCINATION PREVENTION:
- Never fabricate policies, steps, dates, names, or numbers.
- Never fabricate document names or section headings.
- Never generate citations for information not directly present.

CITATION RULES:
- Every factual statement must include an inline citation.
- Each citation must include:
  [Document Name - Section/Heading Name]
- Do not cite anything not explicitly present in the retrieved text.

RESPONSE FORMAT (STRICTLY FOLLOW):

Answer:
<grounded answer with inline citations>

References:
- <Document Name>
"""

# used to improve the chatbot's ability to handle follow-up questions that refer to previous conversation.
async def rewrite_question(question: str, chat_history: str):
    if not chat_history.strip():
        print("No chat history provided, skipping question rewriting.")
        return question

    system_prompt = """
You are a query rewriting assistant.

Your task:
Convert only follow-up questions into fully standalone questions. If they seems to be already standalone, return them as-is.

Rules:
- If the question contains pronouns or vague references
  (such as: this, that, these, those, it, they, them, he, she, the form, the process, the deadline),
  you MUST resolve them using the conversation history.
- Replace the vague reference with the specific subject from the conversation.
- If the question is already standalone, return it EXACTLY as written.
- Do not introduce new topics.
- Do not change the meaning.
- Output ONLY the final standalone question.
"""

    user_message = f"""
Conversation History:
{chat_history}

Latest Question:
{question}
"""

    response = await openai_client.chat.completions.create(
        model=AZURE_OPENAI_CHAT_DEPLOYMENT,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        temperature=0
    )
    rewritten_question = response.choices[0].message.content.strip()
    # print("Chat History for Rewriting:", chat_history)
    print(f"Rewritten Question: {rewritten_question}")

    # Safety fallback
    if not rewritten_question:
        return question

    return rewritten_question


async def generate_answer(question: str, chat_history: str = ""):
    # query_embedding = generate_embedding(question)
    # chunks = retrieve_chunks(question, query_embedding)

    question = await rewrite_question(question, chat_history)
    query_embedding = await generate_embedding(question)
    chunks = await retrieve_chunks(question, query_embedding)

    # print("Retrieved Chunks:", chunks)

    if not chunks:
        print("NO relevant chunks found in retrieval.")
        return "No relevant context found in Backend Files."

    context = "\n\n".join(
        [f"Document: {c['document_name']}\nContent:\n{c['content']}" for c in chunks]
    )

    user_message = f"""
    Question: {question}

    Retrieved Context: {context}    
    """

    response = await openai_client.chat.completions.create(
        model=AZURE_OPENAI_CHAT_DEPLOYMENT,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message}
        ],
        temperature=0
    )

    answer = response.choices[0].message.content
    usage = response.usage
    token_info = {
        "input_tokens": usage.prompt_tokens,
        "output_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens
    }

    return answer, token_info
