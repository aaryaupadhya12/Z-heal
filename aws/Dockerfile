FROM python:3.11-slim

WORKDIR /app

COPY router/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY router/app.py /app/app.py

COPY policy /app/policy

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]