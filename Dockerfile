FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY studio ./studio
COPY assets ./assets
RUN useradd --create-home studio && mkdir /app/data && chown studio:studio /app/data
USER studio
CMD ["python", "-m", "studio"]
