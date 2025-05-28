#!/usr/bin/env python3
"""
Test script to verify large HTTP request handling through the P2P proxy.
This proves that requests > 4KB can be successfully processed.
"""

import asyncio
import aiohttp
import json
import time
import sys

async def test_large_request(base_url="http://localhost:8080"):
    """Test sending progressively larger requests through the proxy"""
    
    test_sizes = [
        (1, "1KB"),
        (4, "4KB"),
        (8, "8KB"),
        (16, "16KB"),
        (32, "32KB"),
        (64, "64KB")
    ]
    
    print("=== Testing Large HTTP Request Handling ===\n")
    
    async with aiohttp.ClientSession() as session:
        for size_kb, label in test_sizes:
            # Create a payload of the specified size
            # Using a realistic JSON structure similar to Trino queries
            large_query = "SELECT * FROM table WHERE " + " OR ".join([
                f"(col1 = 'value{i}' AND col2 = {i} AND col3 = 'data{i}')" 
                for i in range(size_kb * 50)  # Adjust multiplier to reach target size
            ])
            
            payload = {
                "query": large_query,
                "sessionProperties": {
                    "query_max_memory": "1GB",
                    "query_max_memory_per_node": "1GB",
                    "distributed_joins": "true"
                },
                "preparedStatements": {
                    f"stmt{i}": f"SELECT * FROM table{i}" for i in range(10)
                }
            }
            
            json_payload = json.dumps(payload)
            payload_size = len(json_payload.encode('utf-8'))
            
            print(f"\nTest {label}: Payload size = {payload_size} bytes ({payload_size/1024:.2f} KB)")
            
            headers = {
                "X-Trino-User": "test-user",
                "X-Trino-Catalog": "tpch",
                "X-Trino-Schema": "sf1",
                "Content-Type": "application/json",
                "X-Test-Size": label
            }
            
            try:
                start_time = time.time()
                
                # Send the request
                async with session.post(
                    f"{base_url}/v1/statement",
                    headers=headers,
                    data=json_payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    end_time = time.time()
                    duration = end_time - start_time
                    
                    response_text = await response.text()
                    response_size = len(response_text.encode('utf-8'))
                    
                    print(f"  Status: {response.status}")
                    print(f"  Request time: {duration:.3f}s")
                    print(f"  Response size: {response_size} bytes")
                    
                    if response.status == 200:
                        print(f"  ✓ SUCCESS - {label} request processed successfully!")
                        # Calculate chunking info
                        chunks_needed = (payload_size + 1190 - 1) // 1190  # 1190 = max payload per UDP chunk
                        print(f"  UDP chunks required: {chunks_needed}")
                    else:
                        print(f"  ✗ FAILED - Status {response.status}: {response_text[:200]}")
                        
            except asyncio.TimeoutError:
                print(f"  ✗ TIMEOUT - Request timed out after 30s")
            except Exception as e:
                print(f"  ✗ ERROR - {type(e).__name__}: {e}")

    print("\n=== Summary ===")
    print("The HTTP-over-UDP proxy uses chunking to handle large requests:")
    print("- Max UDP packet size: 1200 bytes")
    print("- Max payload per chunk: 1190 bytes")
    print("- Chunking is automatic for any request > 1200 bytes")
    print("- Reassembly timeout: 30 seconds")
    print("\nThis allows handling of requests and responses of ANY size,")
    print("limited only by available memory and network reliability.")

async def test_echo_mode(base_url="http://localhost:8080"):
    """Test echo mode with large payloads to verify chunking"""
    print("\n=== Testing Echo Mode with Large Payload ===\n")
    
    # Create a 10KB test payload
    test_data = {
        "test": "echo",
        "data": "A" * 10000,  # 10KB of A's
        "timestamp": time.time()
    }
    json_payload = json.dumps(test_data)
    payload_size = len(json_payload.encode('utf-8'))
    
    print(f"Echo test payload size: {payload_size} bytes ({payload_size/1024:.2f} KB)")
    
    async with aiohttp.ClientSession() as session:
        try:
            start_time = time.time()
            async with session.post(
                f"{base_url}/echo",
                json=test_data,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                duration = time.time() - start_time
                response_data = await response.json()
                
                if response_data == test_data:
                    print(f"✓ Echo test PASSED - 10KB payload echoed correctly in {duration:.3f}s")
                    chunks = (payload_size + 1190 - 1) // 1190
                    print(f"  Required {chunks} UDP chunks for transport")
                else:
                    print(f"✗ Echo test FAILED - Response doesn't match request")
                    
        except Exception as e:
            print(f"✗ Echo test ERROR - {type(e).__name__}: {e}")

async def main():
    if len(sys.argv) > 1:
        base_url = sys.argv[1]
    else:
        base_url = "http://localhost:8080"
        
    print(f"Testing against: {base_url}")
    
    # Test Trino endpoint with large requests
    await test_large_request(base_url)
    
    # Test echo mode if not in Trino mode
    # await test_echo_mode(base_url)

if __name__ == "__main__":
    asyncio.run(main())