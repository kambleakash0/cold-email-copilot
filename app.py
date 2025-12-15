import os
import json
import requests
import gradio as gr
from google import genai
from google.genai import types
from dotenv import load_dotenv
from pypdf import PdfReader
import urllib.parse
from docx import Document

# --- CONFIGURATION ---
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")
uname = os.getenv("USERNAME", "")
pswd = os.getenv("PASSWORD", "")

# Load sample JD early so it's available when building the Gradio UI (examples)
sample_recruiter_name = "Jane Doe"
sample_company_domain = "example.com"
with open('sample_jd.txt', 'r') as f:
    sample_jd = f.read()

# Initialize client using the actual API key variable
client = genai.Client(api_key=GEMINI_API_KEY)

# --- BACKEND FUNCTIONS ---

def extract_text_from_file(file_obj):
    """Extracts text from PDF, DOCX, TXT, or MD files."""
    if not file_obj:
        return ""
    
    # file_obj is a temp file path string in Gradio 4+. 
    # But usually it's an object. Wait, in Gradio 5/6 it can be a file wrapper or path.
    # In recent Gradio versions, gr.File returns a file path as string or a list of paths.
    # Let's handle it as a path string primarily or check type.
    
    file_path = file_obj.name if hasattr(file_obj, 'name') else file_obj
    ext = os.path.splitext(file_path)[1].lower()
    
    text = ""
    try:
        if ext == '.pdf':
            reader = PdfReader(file_path)
            for page in reader.pages:
                text += page.extract_text()
        elif ext == '.docx':
            doc = Document(file_path)
            for para in doc.paragraphs:
                text += para.text + "\n"
        elif ext in ['.txt', '.md']:
            with open(file_path, 'r', encoding='utf-8') as f:
                text = f.read()
        else:
            return f"Unsupported file format: {ext}"
    except Exception as e:
        return f"Error reading file: {str(e)}"
        
    return text

def find_email_hunter(name, domain):
    """
    Uses Hunter.io API to find a professional email.
    Splits name into First/Last for better accuracy.
    """
    if not name or not domain or not HUNTER_API_KEY:
        return "API Key missing or input empty"
    
    parts = name.split()
    first_name = parts[0]
    last_name = parts[-1] if len(parts) > 1 else ""

    url = f"https://api.hunter.io/v2/email-finder?domain={domain}&first_name={first_name}&last_name={last_name}&api_key={HUNTER_API_KEY}"
    
    try:
        response = requests.get(url)
        data = response.json()
        if "data" in data and data["data"]["email"]:
            return data["data"]["email"]
        else:
            return "Email not found"
    except Exception as e:
        return f"Error: {str(e)}"

def generate_cold_email_content(resume_file, job_description, recruiter_name, company_domain):
    """
    The main logic: 
    1. Reads Resume
    2. Finds Email
    3. Uses GPT to write the email
    """
    # 1. Read Resume
    print("Extracting resume text...")
    resume_text = extract_text_from_file(resume_file)
    if not resume_text:
        return "Please upload a resume.", "", "", "", gr.update(visible=False), gr.update(interactive=False)

    
    print("Resume extracted successfully.")
    # print("Extracted text:\n\n\t", resume_text[:100], "...", "\n")

    # 2. Find Email (Simulated or Real)
    print("Finding email via Hunter.io...")
    recruiter_email = find_email_hunter(recruiter_name, company_domain)

    # 3. Generate Email using Gemini API
    print("Generating cold email content...")
    prompt = f"""
    You are an expert career coach and cold email copywriter.
    
    MY RESUME:
    {resume_text}
    
    JOB DESCRIPTION:
    {job_description}
    
    RECRUITER NAME: {recruiter_name}
    COMPANY: {company_domain}
    
    TASK:
    1. Analyze my resume against the JD. Extract my top matching skills and a 1-sentence summary of why I fit.
    2. Write a cold email subject line.
    3. Write a personalized cold email body. Keep it under 150 words. Be professional but conversational. Mention specific matches between my resume and the JD.
    
    OUTPUT FORMAT:
    Return the result as a JSON string with keys: 'analysis', 'subject', 'body'.
    """

    try:
        completion = client.models.generate_content(
            model="gemini-2.5-flash",
            config=types.GenerateContentConfig(
                temperature=0.7,
                top_p=0.9,
                system_instruction="You are a helpful assistant that outputs JSON.",
                response_mime_type="application/json"
            ),
            contents=prompt
        )
        
        # Safely handle case where completion.text may be None
        text = completion.text if completion.text is not None else ""
        if not text.strip():
            return "Error generating email: empty response from model", recruiter_email, "", "", gr.update(visible=False), gr.update(interactive=False)

        result = json.loads(text)
        # print("Generated content:", json.dumps(result, indent=4))
        
        # extract oputputs
        analysis = result.get("analysis", "Analysis failed")
        body = result.get("body", "No body generated")
        subject = result.get("subject", "No subject")
        
        
        print("Populating Gradio outputs...")
        return (
            json.dumps(analysis, indent=4),
            recruiter_email,
            subject,
            body,
            generate_gmail_link(recruiter_email, subject, body),
            gr.update(interactive=True)
        )


    except Exception as e:
        error_msg = f"Error generating email: {str(e)}"
        if "503" in str(e) or "overloaded" in str(e).lower():
            error_msg = "⚠️ The AI model is currently overloaded. Please wait a moment and try again."
            
        return error_msg, recruiter_email, "", "", gr.update(), gr.update(interactive=False)


def send_via_webhook(email_address, subject, body):
    """
    Sends the email content to an n8n webhook.
    """
    if not N8N_WEBHOOK_URL:
        return "❌ Error: N8N_WEBHOOK_URL not set in .env"
        
    payload = {
        "email_address": email_address,
        "subject": subject,
        "body": body
    }
    
    try:
        response = requests.post(N8N_WEBHOOK_URL, json=payload)
        response.raise_for_status()
        return "✅ Sent to n8n webhook successfully!"
    except Exception as e:
        return f"❌ Error sending to webhook: {str(e)}"

def generate_gmail_link(email, subject, body):
    """Generates a Markdown link to open a Gmail draft."""
    base_url = "https://mail.google.com/mail/?view=cm&fs=1"
    params = {
        "to": email,
        "su": subject,
        "body": body
    }
    query_string = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    url = f"{base_url}&{query_string}"
    return gr.update(value=f"[Open this draft in Gmail]({url})", visible=True)

def check_inputs(resume, jd, name, domain):
    """Checks if all inputs are present to enable the Generate button."""
    if resume and jd and name and domain:
        return gr.update(interactive=True)
    return gr.update(interactive=False)


# --- GRADIO UI LAYOUT ---

with gr.Blocks(title="Cold Email Copilot",
    # theme="Ocean",
    # css="footer {visibility: hidden}",
    ) as demo:
    gr.Markdown("## 📧 Cold Email Copilot")
    gr.Markdown("Paste a JD, upload your resume, and get a personalized cold email.")

    with gr.Row(equal_height=True):
        # --- LEFT COLUMN (Inputs) ---
        with gr.Column(scale=1, variant="panel"):
            resume_input = gr.File(label="Upload your resume (PDF, DOCX, TXT, MD)", file_types=[".pdf", ".docx", ".txt", ".md"])
            jd_input = gr.Textbox(label="Paste Job Description", lines=8, placeholder="Paste the JD here...")
            
            gr.Markdown("### Recruiter Details")
            recruiter_name_input = gr.Textbox(label="Recruiter Name (from LinkedIn)")
            company_domain_input = gr.Textbox(label="Company Domain (e.g., google.com)")
            
            generate_btn = gr.Button("Generate Cold Email", variant="primary", interactive=False)


        # --- RIGHT COLUMN (Outputs) ---
        with gr.Column(scale=2):
            analysis_output = gr.Textbox(label="Role & Skills Analysis", lines=3, interactive=False, max_lines=5)
            
            recruiter_email_output = gr.Textbox(label="Recruiter Email (from Hunter.io)")
            
            subject_output = gr.Textbox(label="Email Subject")
            body_output = gr.Textbox(label="Email Body (editable)", lines=10, interactive=True, max_lines=15)
            
            # formatted_link = gr.State(mailto_link)"
            
            with gr.Row():
                gmail_link = gr.Markdown("[Open this draft in Gmail](#)", visible=False)
                status_msg = gr.Markdown("")

            send_btn = gr.Button("🚀 Send via n8n", interactive=False)


    # --- WIRING ---
    
    # When "Generate" is clicked
    generate_btn.click(
        fn=generate_cold_email_content,
        inputs=[resume_input, jd_input, recruiter_name_input, company_domain_input],
        outputs=[analysis_output, recruiter_email_output, subject_output, body_output, gmail_link, send_btn]
    )


    # When "Send" is clicked
    send_btn.click(
        fn=send_via_webhook,
        inputs=[recruiter_email_output, subject_output, body_output],
        outputs=[status_msg]
    )
    
    # When the email draft subject or body is edited, update the Gmail link
    body_output.change(
        fn=generate_gmail_link,
        inputs=[recruiter_email_output, subject_output, body_output],
        outputs=[gmail_link]
    )
    subject_output.change(
        fn=generate_gmail_link,
        inputs=[recruiter_email_output, subject_output, body_output],
        outputs=[gmail_link]
    )
    recruiter_email_output.change(
        fn=generate_gmail_link,
        inputs=[recruiter_email_output, subject_output, body_output],
        outputs=[gmail_link]
    )
    
    examples = gr.Examples(
        examples=[
            ['sample_resume.pdf', sample_jd, 'Jane Doe', 'example.com'],
            ['sample_resume.docx', sample_jd, 'John Smith', 'example.org'],
            ['sample_resume.txt', sample_jd, 'Alice Wonder', 'example.net'],
            ['sample_resume.md', sample_jd, 'Bob Builder', 'example.io'],
        ],
        inputs=[resume_input, jd_input, recruiter_name_input, company_domain_input],
        example_labels=['PDF Resume', 'Word Resume', 'Text Resume', 'Markdown Resume']
    )

    # Input validation listeners
    input_components = [resume_input, jd_input, recruiter_name_input, company_domain_input]
    for comp in input_components:
        comp.change(fn=check_inputs, inputs=input_components, outputs=generate_btn)


# Run the app
if __name__ == "__main__":
    demo.launch(
        # auth=[(uname, pswd)]
    )