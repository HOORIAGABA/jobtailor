FROM python:3.11-slim

WORKDIR /app

# System deps needed by pdfplumber/docx rendering
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Build the ATS-safe resume template at image build time
RUN python -m app.rendering.build_template

RUN mkdir -p data/outputs data/chroma

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
