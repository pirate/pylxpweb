FROM python:3.13-slim

WORKDIR /app

# Minimal runtime deps for pylxpweb + flask
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        aiohttp>=3.13.2 \
        pydantic>=2.12.0 \
        flask>=3.1.2

# Copy local pylxpweb source (vendored) + the dashboard entrypoint
COPY src/ /app/src/
COPY solar_dashboard.py /app/

EXPOSE 5050

CMD ["python", "-u", "solar_dashboard.py"]
