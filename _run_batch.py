"""Runner script with env setup for batch generation"""
import os
os.environ["AZURE_OPENAI_API_KEY"] = ""

# Now run batch
exec(open("batch_generate.py", encoding="utf-8").read())
