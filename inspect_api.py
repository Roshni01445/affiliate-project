from gradio_client import Client

SPACE_ID = "r3gm/wan2-2-fp8da-aoti-preview" 

print(f"Connecting to {SPACE_ID}...")

client = Client(SPACE_ID)
client.view_api()