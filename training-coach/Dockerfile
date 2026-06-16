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

# Launch via with-contenv (run.sh) so SUPERVISOR_TOKEN is available at runtime
COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 8000

CMD ["/run.sh"]
