from datetime import datetime

file_handle = None


def open_file():
    global file_handle

    assert file_handle is None

    start_time = datetime.now()
    date = start_time.strftime("%Y%m%d_%H%M%S")
    name = f"./data/datadump_{date}.txt"

    print(f"Opening file {name}")

    file_handle = open(name, "w")


def write_to_file(data: list):
    if file_handle is None:
        open_file()

    for item in data:
        print(item, file=file_handle)
