FROM python:3.13-alpine
WORKDIR /app
COPY pf_watchdog.py .
# A named volume mounted at /state on first use inherits this ownership.
RUN mkdir /state && chown 65534:65534 /state
USER 65534:65534
VOLUME /state
CMD ["python", "/app/pf_watchdog.py"]
