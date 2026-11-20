POST /items returns 200 when an item is created; it should return 201 Created.
Creating an item whose name already exists returns 400, but it should be 409 Conflict.
