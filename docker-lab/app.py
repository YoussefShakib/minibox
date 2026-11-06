import os

import redis

r = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), port=6379)
print(f"visit number {r.incr('visits')}")