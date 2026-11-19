# from azure.search.documents import SearchClient
from azure.search.documents.aio import SearchClient
from azure.core.credentials import AzureKeyCredential
from FAA_MedCert_Case_Builder.shared.config import *

search_client = SearchClient(
    endpoint=AZURE_SEARCH_ENDPOINT,
    index_name=AZURE_SEARCH_INDEX,
    credential=AzureKeyCredential(AZURE_SEARCH_KEY),
    api_version="2025-11-01-preview"
)