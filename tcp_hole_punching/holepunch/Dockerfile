FROM python:3.9-slim
# Or python:3.10-slim / python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
CMD ["python", "-u", "main.py"] 