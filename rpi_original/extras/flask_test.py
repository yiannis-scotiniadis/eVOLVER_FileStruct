from flask import Flask, request, jsonify, abort
import json
import thread
import time

app = Flask(__name__)
app.debug = False  # Turn this off when you run it for real!

######load default last settings
#in_file = open("data.txt","r")
#data = json.load(in_file)
#in_file.close()

data = {"parameter": "value"}

def hello_world(delay):
        print "starting!!"
        time.sleep(delay)
        print "hello world"

@app.before_request
def limit_remote_addr():
    if request.remote_addr != '155.41.66.90':
        abort(403)  # Forbidden

@app.route("/update/<param>", methods=['GET', 'POST'])
def update(param):
    if request.method == 'POST':

        thread.start_new_thread( hello_world, (10,) )

        data.update(request.json)

        with open('data.txt', 'w') as outfile:
                json.dump(data, outfile)

        print(data)
        return "something you compute"

    elif request.method == 'GET':
        print("redo as a POST request please")
        return ""

@app.route("/query/<param>")
def query(param):
    return "data you get from wherever it lives"

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=80)

