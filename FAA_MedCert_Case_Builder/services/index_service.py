import uuid
import asyncio
from FAA_MedCert_Case_Builder.shared.search_client import search_client

async def delete_by_session(session_id: str):
    try:
        results = await search_client.search(
            search_text="*",
            filter=f"session_id eq '{session_id}'"
        )
        docs = []
        async for r in results:
            if "id" in r:
                docs.append({"id": r["id"]})

        if docs:
            for i in range(0, len(docs), 1000):
                await search_client.delete_documents(documents=docs[i:i+1000])
    except Exception as e:
        print(f"Error deleting embeddings: {e}")

async def index_chunks(chunks, document_path, embedder, session_id):
    async def process_chunk(chunk):
        embedding = await embedder(chunk)
        return {
            "id": str(uuid.uuid4()),
            "content": chunk,
            "document_path": document_path,
            "session_id": session_id,
            "embedding": embedding
        }
    documents = await asyncio.gather(*[process_chunk(chunk) for chunk in chunks])

    if documents:
        await search_client.upload_documents(documents=documents)