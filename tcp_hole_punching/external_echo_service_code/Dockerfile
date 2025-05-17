FROM python:3.9-slim

WORKDIR /app

# requirements.txt is empty for now, but good practice to include COPY
COPY requirements.txt .
# RUN pip install --no-cache-dir -r requirements.txt

COPY external_echo_server.py .

# Cloud Run will set the PORT env var, server.py will use it.
# Default to 8080 if not set, though Cloud Run always sets it.
ENV PORT 8080

EXPOSE 8080

CMD ["python", "./external_echo_server.py"] 