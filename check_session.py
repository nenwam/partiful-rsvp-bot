"""Verify a saved partiful_state.json actually carries an auth record."""
import json, sys
st=json.load(open("partiful_state.json"))
origins=st.get("origins",[])
idb_ok=False; fb=[]
for o in origins:
    if o.get("origin","").endswith("partiful.com"):
        for db in o.get("indexedDB", []) or []:
            if "firebase" in json.dumps(db)[:2000].lower():
                idb_ok=True
        fb=[e["name"] for e in o.get("localStorage",[]) if "firebase" in e["name"].lower()]
print(f"cookies              : {len(st.get('cookies',[]))}")
print(f"partiful origins     : {[o.get('origin') for o in origins]}")
print(f"indexedDB captured   : {any('indexedDB' in o for o in origins)}")
print(f"firebase auth in IDB : {idb_ok}")
print(f"firebase in localSt. : {fb or 'none'}")
print()
print("VERDICT:", "logged in ✓" if (idb_ok or fb) else "LOGGED OUT ✗ — re-run login")
