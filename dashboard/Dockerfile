FROM python:3.13-slim
WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY *.py ./dashboard/
COPY static ./dashboard/static/
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python", "-m", "uvicorn"]
CMD ["dashboard.server:app", "--host", "0.0.0.0", "--port", "8080"]
