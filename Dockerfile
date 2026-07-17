FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py *.json config.yaml chains.yaml ./

# Health check server listens on 8080
EXPOSE 8080

CMD ["python", "scanner.py"]
