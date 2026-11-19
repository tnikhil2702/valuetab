import uuid
from Playbook_chatbot.shared.search_client import search_client

async def delete_existing(filename: str):

    results = await search_client.search(
        search_text="*",
        filter=f"document_name eq '{filename}'"
    )

    # docs = [{"id": r["id"]} for r in results]
    docs = []
    async for r in results:
        docs.append({"id": r["id"]})

    if docs:
        await search_client.delete_documents(documents=docs)


async def index_chunks(chunks, filename, embedder, section, department):

    documents = []

    for chunk in chunks:

        embedding = await embedder(chunk)

        documents.append({
            "id": str(uuid.uuid4()),
            "content": chunk,
            "embedding": embedding,
            "document_name": filename,
            "section": section,
            "version": "v1",
            "department": department
        })

    # search_client.upload_documents(documents)
    if documents:
        await search_client.upload_documents(documents=documents)
