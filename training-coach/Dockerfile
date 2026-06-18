ARG BUILD_FROM
FROM $BUILD_FROM

# The base-python image already provides Python 3.12 + pip. Only add tzdata so
# datetime.now() honors the TZ Supervisor passes (the morning check gates on
# local time). Do NOT apk add python3 here — it would pull Alpine's newer system
# Python alongside the base one and break dependency wheels.
RUN apk add --no-cache tzdata

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
