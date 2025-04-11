# Use the official Python 3.9 slim image
FROM python:3.9-slim

# Set working directory inside the container
WORKDIR /usr/src/app

# Install required Python packages for Google API access
RUN pip install --no-cache-dir \
    google-auth \
    google-auth-httplib2 \
    google-auth-oauthlib \
    google-api-python-client

# Copy the upload script into the container
COPY main.py .

# Make the upload script executable
RUN chmod +x main.py

# Set the entrypoint to run the upload script when the container starts
ENTRYPOINT ["./main.py"]
