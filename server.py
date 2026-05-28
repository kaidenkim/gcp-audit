import sys, os
DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)
os.chdir(DIR)
import uvicorn
uvicorn.run("main:app", host="0.0.0.0", port=9090, reload=False)
