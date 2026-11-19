from Playbook_chatbot.shared.openai_client import client as openai_client
from Playbook_chatbot.shared.config import *

async def generate_embedding(text: str):

    response = await openai_client.embeddings.create(
        model=AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
        input=text
    )

    return response.data[0].embedding
