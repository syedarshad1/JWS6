ssh -p 22087 YOUR_USERNAME@127.0.0.5 curl -s -o /dev/null -w '%{http_code}' --max-time 2 http://100.72.160.193:12001/
