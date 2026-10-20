"""Native messaging dispatcher; errors contain fixed codes, never source values."""
import struct
import uuid
from . import VERSION
from .contract import MAX_MESSAGE, decode, encode, keys, UUID
from .security import CollectorError


def read_exact(stream, length):
    result = bytearray()
    while len(result) < length:
        part = stream.read(length - len(result))
        if not part:
            raise CollectorError("TRUNCATED_FRAME")
        result.extend(part)
    return bytes(result)


def read_message(stream):
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        header += read_exact(stream, 4 - len(header))
    length = struct.unpack("<I", header)[0]
    if not 0 < length <= MAX_MESSAGE:
        raise CollectorError("FRAME_SIZE")
    return decode(read_exact(stream, length))


def write_message(stream, value):
    data = encode(value)
    if len(data) > MAX_MESSAGE - 1024:
        data = encode({"id": value.get("id"), "ok": False, "error": "OUTPUT_SIZE"})
    stream.write(struct.pack("<I", len(data)) + data)
    stream.flush()


class Dispatcher:
    def __init__(self, store):
        self.store = store
        self.active = {}
        self.repair_plans = {}
        self.kinds = {}
        self.gaps = None

    def call(self, request):
        rid = request.get("id") if isinstance(request, dict) else None
        rid = rid if isinstance(rid, str) and UUID.fullmatch(rid) else None
        try:
            keys(request, {"protocol", "id", "op", "args"})
            if request["protocol"] != "b-native/1" or rid is None:
                raise CollectorError("PROTOCOL_VERSION")
            op, a = request["op"], request["args"]
            if op == "hello":
                keys(a, set())
                result = {"version": VERSION, "protection": "CurrentUser-DPAPI", "A_connected": False, "review_supported": True, 'base_repair_supported': True, 'gap_repair_supported': True, 'gap_client': 'b-gap-client/1'}
            elif op == 'gap_inspect':
                keys(a, {'run_id'})
                if self.active:
                    raise CollectorError('TASK_ALREADY_RUNNING')
                if self.gaps is None:
                    from .gaps import GapBridge
                    self.gaps = GapBridge(self.store)
                result = self.gaps.inspect(a['run_id'])
            elif op == 'gap_open':
                keys(a, {'run_id', 'client'})
                if self.active:
                    raise CollectorError('TASK_ALREADY_RUNNING')
                if self.gaps is None:
                    from .gaps import GapBridge
                    self.gaps = GapBridge(self.store)
                try:
                    result = self.gaps.open(a['run_id'], a['client'])
                finally:
                    # Preserve ownership even if provenance and cleanup both fail.
                    if self.gaps.owned:
                        owned = self.gaps.owned
                        self.active[owned['run_id']] = owned['generation']
                        self.kinds[owned['run_id']] = 'gap'
                self.repair_plans.clear()
            elif op in ('gap_claim', 'gap_part', 'gap_commit', 'gap_finish'):
                fields = {'run_id', 'generation'} | {
                    'gap_claim': set(), 'gap_part': {'op_id','attempt','offset','items'},
                    'gap_commit': {'op_id','attempt','count'}, 'gap_finish': {'reason'}
                }[op]
                keys(a, fields)
                run, generation = a['run_id'], a['generation']
                if self.active.get(run) != generation or self.kinds.get(run) != 'gap':
                    raise CollectorError('TASK_NOT_OWNED')
                rest = {k:v for k,v in a.items() if k not in ('run_id','generation')}
                result = getattr(self.gaps, op[4:])(run, generation, **rest)
                if op == 'gap_finish':
                    self.active.pop(run, None)
                    self.kinds.pop(run, None)
            elif op == 'repair_scan':
                keys(a, {'parent_run', 'plan_id', 'offset'})
                if self.active:
                    raise CollectorError('TASK_ALREADY_RUNNING')
                key = a['plan_id']
                if key is None:
                    if a['offset'] != 0:
                        raise CollectorError('REPAIR_CURSOR')
                    self.repair_plans.clear()
                    key, plan = uuid.uuid4().hex, None
                else:
                    plan = self.repair_plans.get(key)
                    if plan is None or plan.offset != a['offset'] or plan.parent != a['parent_run']:
                        raise CollectorError('REPAIR_CURSOR')
                plan = self.store.repair_scan_page(a['parent_run'], plan)
                self.repair_plans[key] = plan
                done = plan.offset == plan.total
                result = {'plan_id': key, 'next_offset': plan.offset, 'total': plan.total, 'done': done}
                if done:
                    ready = plan.finish()
                    result.update(target_count=len(ready['rooms']), unlocated_failures=ready['unlocated_failures'], other_diagnostics=ready['other_diagnostics'])
            elif op == 'repair_discard':
                keys(a, {'plan_id'})
                self.repair_plans.pop(a['plan_id'], None)
                result = {'discarded': True}
            elif op == 'repair_start':
                keys(a, {'plan_id'})
                if self.active:
                    raise CollectorError('TASK_ALREADY_RUNNING')
                plan = self.repair_plans.get(a['plan_id'])
                if plan is None:
                    raise CollectorError('REPAIR_NOT_READY')
                result = self.store.start_base_repair(plan)
                self.active[result['run_id']] = result['generation']
                self.kinds[result['run_id']] = 'base'
                self.repair_plans.clear()
            elif op == 'repair_targets':
                keys(a, {'run_id', 'generation', 'offset'})
                if self.active.get(a['run_id']) != a['generation'] or self.kinds.get(a['run_id']) != 'base':
                    raise CollectorError('TASK_NOT_OWNED')
                result = self.store.repair_targets(a['run_id'], a['generation'], a['offset'])
            elif op == "start":
                keys(a, {"scope"})
                if self.active:
                    raise CollectorError("TASK_ALREADY_RUNNING")
                result = self.store.start(a["scope"])
                self.active[result["run_id"]] = result["generation"]
                self.kinds[result["run_id"]] = "ordinary"
            elif op == "append":
                keys(a, {"run_id", "generation", "batch_id", "items"})
                if self.active.get(a["run_id"]) != a["generation"] or self.kinds.get(a["run_id"]) == "gap":
                    raise CollectorError("TASK_NOT_OWNED")
                result = self.store.append(a["run_id"], a["generation"], a["batch_id"], a["items"])
            elif op == "finish":
                keys(a, {"run_id", "generation", "reason"})
                if self.active.get(a["run_id"]) != a["generation"] or self.kinds.get(a["run_id"]) == "gap":
                    raise CollectorError("TASK_NOT_OWNED")
                result = self.store.finish(a["run_id"], a["generation"], a["reason"])
                self.active.pop(a["run_id"], None)
                self.kinds.pop(a["run_id"], None)
            elif op == "list_runs":
                keys(a, set())
                result = {"runs": self.store.list_runs()}
            elif op == "preview":
                keys(a, {"run_id", "offset"})
                result = self.store.preview(a["run_id"], a["offset"])
            elif op == "review_page":
                keys(a, {"run_id", "offset", "total"})
                result = self.store.review_page(a["run_id"], a["offset"], a["total"])
            elif op == "export":
                keys(a, {"run_id"})
                result = self.store.export(a["run_id"])
            elif op == "delete_run":
                keys(a, {"run_id"})
                result = self.store.delete_run(a["run_id"])
            else:
                raise CollectorError("OP_NOT_ALLOWED")
            return {"id": rid, "ok": True, "result": result}
        except CollectorError as error:
            return {"id": rid, "ok": False, "error": str(error)}
        except Exception:
            return {"id": rid, "ok": False, "error": "LOCAL_OPERATION_FAILED"}

    def disconnect(self):
        self.repair_plans.clear()
        for run, generation in list(self.active.items()):
            try:
                self.store.finish(run, generation, "disconnected")
            except Exception:
                pass
        self.active.clear()
        self.kinds.clear()
        if self.gaps:
            self.gaps.clear()
