ARG BUILD_FROM
FROM $BUILD_FROM

# Install Python (tzdata so datetime.now() honors the TZ Supervisor passes,
# instead of falling back to UTC — the morning check gates on local time)
RUN apk add --no-cache python3 py3-pip tzdata

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# Copy application
COPY app/ .

EXPOSE 8000

CMD ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
