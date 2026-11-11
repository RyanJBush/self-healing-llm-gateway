# Architecture

Three views: the parts, one request's decisions, and one provider's circuit.
Source of truth for behaviour is `openapi.yaml`, `DECISIONS.md`, and the tests.

## 1. Components

```mermaid
flowchart LR
    Client["Client<br/>localhost only, no auth"] -->|"POST /v1/generations"| App["ASGI app (app.py)<br/>deadline starts here<br/>body read, disconnect watch"]
    App -->|"raw body"| Val["Validation (validation.py)<br/>one 422 envelope"]
    Val -->|"ParsedRequest"| Eng["Engine (engine.py)<br/>retry, fallback, deadline,<br/>Retry-After, classification"]
    Eng -->|"acquire / settle"| BP["Breaker: primary<br/>(breaker.py)"]
    Eng -->|"acquire / settle"| BF["Breaker: fallback<br/>(breaker.py)"]
    Eng -->|"call(text, ctx), per-call timeout"| PP["Provider: primary<br/>stub or HTTP"]
    Eng -->|"call(text, ctx), per-call timeout"| PF["Provider: fallback<br/>stub or HTTP"]
    Eng -.->|"trace events"| Log[("gateway.events log<br/>no prompts, outputs,<br/>credentials, upstream bodies")]
    BP -.->|"transitions"| Log
    BF -.->|"transitions"| Log
    Clock["Event-loop monotonic clock<br/>(SimulatedLoop in tests)"] -.-> App
    Clock -.-> Eng
    Wall["Wall clock<br/>(only to parse HTTP-date Retry-After)"] -.-> Eng
```

## 2. One request

```mermaid
flowchart TD
    Start(["Handler entry<br/>deadline = now + 2000 ms"]) --> Body["Read body within the deadline"]
    Body -->|"disconnect"| Cancel(["Cancel work, no response,<br/>no further calls"])
    Body -->|"deadline passed"| E504a(["504 deadline_exceeded, 0 calls"])
    Body --> Valid{"Valid JSON, fields, route?"}
    Valid -->|"no"| E422(["422 invalid_request, 0 calls"])
    Valid -->|"yes"| Pcheck{"Deadline left?<br/>Primary circuit admits?"}
    Pcheck -->|"deadline reached"| E504b(["504 deadline_exceeded"])
    Pcheck -->|"open or probe in flight"| Fcheck
    Pcheck -->|"admitted"| Pcall["Primary call<br/>timeout = min(500 ms, remaining)"]
    Pcall --> Pres{"Outcome"}
    Pres -->|"success"| OK(["200 from primary"])
    Pres -->|"other 4xx or malformed 2xx"| E502(["502 upstream_rejected<br/>no retry, no fallback"])
    Pres -->|"timeout, connection failure,<br/>408, 429, 5xx"| Retry{"First primary call,<br/>not a probe, wait fits<br/>before the deadline?"}
    Retry -->|"yes: wait max(100 ms, Retry-After),<br/>wait cut off at the deadline"| Pcheck
    Retry -->|"no"| Fcheck{"Deadline left?<br/>Fallback circuit admits?"}
    Fcheck -->|"deadline reached"| E504b
    Fcheck -->|"open or probe in flight"| E503(["503 providers_unavailable"])
    Fcheck -->|"admitted"| Fcall["Fallback call, once<br/>timeout = min(500 ms, remaining)"]
    Fcall --> Fres{"Outcome"}
    Fres -->|"success"| OKF(["200 from fallback"])
    Fres -->|"other 4xx or malformed 2xx"| E502
    Fres -->|"qualifying failure"| End{"Deadline reached?"}
    End -->|"yes"| E504b
    End -->|"no"| E503
```

Never returns from fallback to primary. At most 3 calls (primary 2, fallback 1). A failed
fallback ends the request. An unexpected exception anywhere is a sanitized 500 and is never
retried.

## 3. Circuit breaker (one per provider, in memory)

```mermaid
stateDiagram-v2
    [*] --> Closed
    Closed --> Open: 2 consecutive qualifying failures
    Open --> HalfOpen: cooldown of 5000 ms elapsed and the next request takes the single probe slot
    HalfOpen --> Closed: probe succeeds and count resets
    HalfOpen --> Open: probe fails with a qualifying failure and cooldown restarts
    HalfOpen --> Open: probe cancelled or non-qualifying, slot released, fresh cooldown, no recovery claimed
```

Qualifying failure: connection failure, timeout, 408, 429, 5xx. Other 4xx and malformed
successful responses neither increment nor reset the count. A success resets it. Completions
from calls admitted under an older generation are ignored.
