#!/usr/bin/env python3
"""Test script to verify large HTTP request handling through the proxy"""

import requests
import json
import sys

def test_large_request(url="http://localhost:8080"):
    """Test sending a large request through the proxy"""
    
    # Create a large payload (> 4KB)
    large_data = {
        "query": "SELECT * FROM large_table WHERE " + " OR ".join([f"id = {i}" for i in range(1000)]),
        "metadata": {
            "description": "This is a test query with a large payload to verify UDP chunking",
            "data": "x" * 5000  # 5KB of data
        }
    }
    
    payload = json.dumps(large_data)
    payload_size = len(payload.encode('utf-8'))
    
    print(f"Testing large request handling...")
    print(f"Payload size: {payload_size} bytes ({payload_size / 1024:.2f} KB)")
    
    headers = {
        "X-Trino-User": "admin",
        "X-Trino-Catalog": "tpch", 
        "X-Trino-Schema": "sf1",
        "Content-Type": "application/json"
    }
    
    try:
        # Send the large request
        response = requests.post(f"{url}/v1/statement", headers=headers, data=payload)
        
        print(f"\nResponse status: {response.status_code}")
        print(f"Response headers: {dict(response.headers)}")
        
        if response.status_code == 200:
            print("✓ Large request successfully processed!")
            print(f"Response preview: {response.text[:200]}...")
        else:
            print(f"✗ Request failed: {response.text}")
            
    except Exception as e:
        print(f"✗ Error: {e}")

if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
    test_large_request(url)