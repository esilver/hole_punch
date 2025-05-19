# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container at /app
# Ensure requirements.txt is in the same directory as the Dockerfile when building
COPY requirements.txt ./

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy all Python source files and the HTML file into the container at /app
COPY main.py ./
COPY quic_tunnel.py ./
COPY network_utils.py ./
COPY ui_server.py ./
COPY p2p_protocol.py ./
COPY rendezvous_client.py ./
COPY index.html ./
# Include certificates for QUIC connections
COPY cert.pem ./
COPY key.pem ./

# Make port 8080 available to the world outside this container (Cloud Run default)
EXPOSE 8080

# Define environment variable for the port (Cloud Run will set this)
ENV PORT=8080

# Run main.py when the container launches
# Use unbuffered Python output for better logging in Cloud Run
CMD ["python", "-u", "main.py"] 