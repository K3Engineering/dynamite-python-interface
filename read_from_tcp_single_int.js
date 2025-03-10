// Read data from TCP and put in on a reference scope channel

// TCP
print("start");
if(!('Scope' in this)) throw "Please open a Scope instrument";

var server = Tcp();
if(!server.listen("localhost", 8080)) throw "server error";
print("Waiting for connection, connect on the python side");
if(!server.isConnected() && !server.waitForNewConnection(20.0)) throw "server wait timeout";

channelData = [];
maxLenData = 8000; // How many values to buffer


var i = 0;
while(1){

    //var availableBytes = server.waitAvailable(1, 5.0);

    //var inputVals = server.readText().trim().split("\n").map(Number);
    //print("vals", inputVals);
    var inputVals = server.readInt(0);

    Array.prototype.push.apply(channelData, inputVals);

    // keep only the latest data, and remove old data
    while(channelData.length > maxLenData){
        channelData.shift();
    }
    // print(channelData);

    Scope.Ref1.setData(channelData, 1000);
    wait(0.001);
}

server.disconnect();
server.close();
