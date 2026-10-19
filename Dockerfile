FROM python:3.11-slim

WORKDIR /app

# stdlib-only, but keep the step so a future dependency lands cleanly
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8777

EXPOSE 8777

CMD ["python", "app.py"]
