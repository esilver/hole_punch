# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Install necessary libraries
RUN pip install --no-cache-dir Flask requests gunicorn

# Copy the current directory contents into the container at /app
COPY main.py .

# Make port 8080 available to the world outside this container
# Cloud Run will automatically use this port if not specified otherwise by PORT env var.
EXPOSE 8080

# Define environment variable (Cloud Run will set this, but good for explicitness)
ENV PORT 8080

# Run the web server using Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "main:app"] 