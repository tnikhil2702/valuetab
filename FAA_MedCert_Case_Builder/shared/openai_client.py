# from openai import AzureOpenAI
from openai import AsyncAzureOpenAI
from FAA_MedCert_Case_Builder.shared.config import *

client = AsyncAzureOpenAI(
    api_key=AZURE_OPENAI_KEY,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    # api_version="2024-02-15-preview"
    api_version="2024-12-01-preview"
)
