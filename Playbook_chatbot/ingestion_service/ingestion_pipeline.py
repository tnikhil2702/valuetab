from Playbook_chatbot.ingestion_service.blob_service import blob_exists, delete_blob, upload_blob
from Playbook_chatbot.ingestion_service.parser_service import extract_text, split_by_main_headings
from Playbook_chatbot.ingestion_service.chunk_service import chunk_text
from Playbook_chatbot.ingestion_service.embed_service import generate_embedding
from Playbook_chatbot.ingestion_service.index_service import delete_existing, index_chunks

async def process_upload(file, department: str, regenerate: bool =False):

    file_bytes = await file.read()
    filename = file.filename

    exists = await blob_exists(filename)

    if exists and not regenerate:
        return {"status": "This file is already present in backend azure storage, click to regenerate if you want to reingest it.", "file": filename}


    if regenerate and exists:
        await delete_blob(filename)
        await delete_existing(filename)
    await upload_blob(file_bytes, filename)

    text = extract_text(file_bytes, filename)
    sections = await split_by_main_headings(text)

    if sections:
        print(f"Detected {len(sections)} structured sections")
        for heading, content in sections:
            if not content.strip():
                continue
            chunks = chunk_text(content)
            await index_chunks(
                chunks=chunks,
                filename=filename,
                embedder=generate_embedding,
                section=heading,
                department=department
            )
    else:
        print("No structural headings found. Falling back to sliding chunking.")
        chunks = chunk_text(text)
        await index_chunks(
            chunks=chunks,
            filename=filename,
            embedder=generate_embedding,
            section="General",
            department=department
        )
    return {"status": "File Ingested and Indexed Successfully!", "file": filename}
