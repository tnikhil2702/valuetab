import os
from pinecone import Pinecone, ServerlessSpec
from sentence_transformers import SentenceTransformer
from dotenv import load_dotenv
import hashlib
load_dotenv()

# ------------------ ENV ------------------
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
pc = Pinecone(api_key=PINECONE_API_KEY)

PINECONE_INDEX = "email2ai"

if os.getenv("CREATE_PINECONE_INDEX") == "true":
    if PINECONE_INDEX not in pc.list_indexes().names():
        pc.create_index(
            name=PINECONE_INDEX,
            dimension=384,
            metric="cosine",
            spec=ServerlessSpec(
                cloud="aws",
                region="us-east-1"
            )
        )

index = pc.Index(PINECONE_INDEX)
def make_vector_id(file_id: str, chunk_index: int) -> str:
    raw = f"{file_id}_{chunk_index}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# model load 
_embedding_model = SentenceTransformer(
    "abhinand/MedEmbed-small-v0.1"
)

def get_embedding_model() -> SentenceTransformer:
    return _embedding_model

# Embedding function 
def embed_documents(texts: list[str]) -> list[list[float]]:
    model = get_embedding_model()
    return model.encode(
        texts,
        batch_size=64,
        normalize_embeddings=True,
        show_progress_bar=False
    ).tolist()


def embed_query(text: str) -> list[float]:
    model = get_embedding_model()
    return model.encode(
        text,
        normalize_embeddings=True
    ).tolist()

# ---- Upsert ---- # 
def upsert_chunks(chunks: list[str], file_id: str, filename: str, batch_size=100):

    print(f"[UPSERT] Starting overwrite for {filename}")
    
    delete_file_vectors(file_id)

    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        embeddings_batch = embed_documents(batch)

        vectors = []
        for j, emb in enumerate(embeddings_batch):
            chunk_index = i + j
            vectors.append({
                # "id": f"{file_id}_{chunk_index}",  # unique ID per chunk
                "id": make_vector_id(file_id, chunk_index),
                "values": emb,
                "metadata": {
                    "file_id": file_id,
                    "filename": filename,
                    "chunk_index": chunk_index,
                    "text": batch[j]
                }
            })

        index.upsert(vectors=vectors)
    print(f"[UPSERT] Completed upsert for {filename}")

# ---- Query ---- # 
def query_chunks(query, filename, file_id, top_k=15):
    q_emb = embed_query(query)
    res = index.query(
        vector=q_emb,
        top_k=top_k,
        include_metadata=True,
        filter={
            "filename": filename,
            "file_id": file_id
        }
    )

    return [m["metadata"]["text"] for m in res.get("matches", [])] 

def delete_file_vectors(file_id: str): 
    print(f"[DELETE] Attempting to delete all vectors for file_id='{file_id}'...")
    try:
        index.delete(filter={"file_id": file_id})
        print(f"Delete request sent successfully for file_id='{file_id}'")
    except Exception as e:
        print(f"Failed to delete vectors for file_id='{file_id}': {e}")