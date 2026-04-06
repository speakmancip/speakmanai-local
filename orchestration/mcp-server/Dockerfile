FROM python:3.11-slim

WORKDIR /app

# Install system dependencies if required by motor/httpx
RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
# Ensure google-genai is included for Vertex AI
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY . .

# Start the FastMCP server via Uvicorn
EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]