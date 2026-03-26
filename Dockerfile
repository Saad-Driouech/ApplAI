FROM python:3.12-slim

WORKDIR /app

# System deps: pdflatex for CV compilation
RUN apt-get update && apt-get install -y --no-install-recommends \
    texlive-latex-base \
    texlive-latex-recommended \
    texlive-fonts-recommended \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir fastapi uvicorn[standard]

COPY src/ ./src/

CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000"]
