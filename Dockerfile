FROM python:3.9-slim

WORKDIR /usr/src/app

# Install required Python packages for Google API access
RUN pip install --no-cache-dir \
    google-auth \
    google-auth-httplib2 \
    google-auth-oauthlib \
    google-api-python-client

# Copy the main.py file into the container
COPY main.py .

# Mark the script as executable
RUN chmod +x main.py

# Set the entrypoint to run main.py when the container starts
ENTRYPOINT ["./main.py"]
