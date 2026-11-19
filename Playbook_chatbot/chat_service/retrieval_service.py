from azure.search.documents.models import VectorizedQuery
from Playbook_chatbot.shared.search_client import search_client


async def retrieve_chunks(question, embedding, top_k=40):

    vector_query = VectorizedQuery(
        vector=embedding,
        k_nearest_neighbors=top_k,
        fields="embedding"
    )

    search_results = await search_client.search(
        search_text=question,
        vector_queries=[vector_query],
        top=top_k,
        # query_type="semantic",
        # semantic_configuration_name="semantic-config",
        # semantic_query=question,
        # query_caption="extractive",
        # query_answer="extractive",
    )

    results = []

    async for r in search_results:
        results.append({
            "content": r["content"],
            "document_name": r["document_name"],
            "section": r.get("section"),
        })

    return results
