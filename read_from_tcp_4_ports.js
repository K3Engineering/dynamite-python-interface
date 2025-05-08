// Read data from TCP and put in on a reference scope channel
// Requires waveforms >= 3.23.54 (2025-03-05)
// https://forum.digilent.com/topic/8908-waveforms-beta-download/

// TCP
print("start");
if(!('Scope' in this)) throw "Please open a Scope instrument";

var ports = [8080, 8081, 8082, 8083];
var servers = [Tcp(),Tcp(),Tcp(),Tcp()];

//var server = Tcp();
for( var i = 0; i < 4; i++){
    print("server num", i, ports[i]);
    if(!servers[i].listen("localhost", ports[i])) throw "server error";
    print("Waiting for connection, connect on the python side");
    if(!servers[i].isConnected() && !servers[i].waitForNewConnection(20.0)) throw "server wait timeout";
}

// The first TCP sends the scaling factor to use
wait(0.1);
var scaleFactor = [1,1,1,1];
for( var i = 0; i < 4; i++) {
    scaleFactor[i] = servers[i].readInt(1);
    print("Scale factor", i, scaleFactor[i]);
}


//channelData = [];
channelDatas = [[],[],[],[]];
scopes = [Scope.Ref1, Scope.Ref2, Scope.Ref3, Scope.Ref4];
maxLenData = 8000; // How many values to buffer


var i = 0;
while(1){

    for( var i = 0; i < 4; i++)
    {
        
        var inputVals = servers[i].readInt(0);

        // Scale each value
        for( var inputI = 0; inputI < inputVals.length; inputI++) {
            inputVals[inputI] /= scaleFactor[i];
        }
        //inputVals.map(element => element / scaleFactor);
    
        Array.prototype.push.apply(channelDatas[i], inputVals);
    
        // keep only the latest data, and remove old data
        while(channelDatas[i].length > maxLenData){
            channelDatas[i].shift();
        }
    
        scopes[i].setData(channelDatas[i], 1000);
        wait(0.001);
    }
    
}

server.disconnect();
server.close();
