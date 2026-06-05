"""Quick test: parser + API connectivity"""
import asyncio, json, time, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from xiaoye.llm import LLMClient

API_KEY = open(".env", encoding="utf-8").readline().strip().split("=", 1)[1].strip('"').strip("'")

async def test_parse():
    client = LLMClient(API_KEY)
    now = time.time()
    
    # Test valid response
    msgs = client._parse_response(
        '{"messages":[{"t":3,"text":"cao"},{"t":4,"text":"wo ye"}]}', now
    )
    t1 = msgs[0].send_at - now
    t2 = msgs[1].send_at - now
    assert 2.5 < t1 < 3.5, f"msg1 timing: {t1}"
    assert 6.5 < t2 < 7.5, f"msg2 timing: {t2}"
    print(f"OK Parse: {len(msgs)} msgs, gaps: {t1:.1f}s, {t2:.1f}s")

    # Test plain text fallback
    msgs2 = client._parse_response("haha lol\n\ndude what up", now)
    assert len(msgs2) == 2
    print(f"OK Plain text: {len(msgs2)} msgs")

    # Test empty
    msgs3 = client._parse_response("", now)
    assert len(msgs3) == 1
    print(f"OK Empty: {msgs3[0].text}")

    print("All tests passed!")

asyncio.run(test_parse())

