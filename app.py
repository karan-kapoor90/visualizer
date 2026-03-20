import os
import asyncio
import time
import json
import urllib.request
from fastapi import FastAPI, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from kubernetes import client, config, dynamic
from kubernetes.client import api_client
import uvicorn
import yaml

app = FastAPI()
# WebSocket Connection Manager
active_connections: list[WebSocket] = []

async def broadcast_message(message: str):
    for connection in active_connections:
        try:
            await connection.send_text(message)
        except Exception:
            pass

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    print("New websocket connection")
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_connections.remove(websocket)

@app.websocket("/ws/yaml")
async def websocket_yaml_endpoint(websocket: WebSocket):
    print("New websocket YAML connection")
    await websocket.accept()
    try:
        kubeconfig_file = os.getenv("KUBECONFIG")
        if not kubeconfig_file and os.path.exists("kdp_kube_config"):
            kubeconfig_file = "kdp_kube_config"
            
        # Read context from query parameters if supplied
        target_context = websocket.query_params.get("context", "")
        if not target_context or target_context == "current":
             try:
                 _, active = config.list_kube_config_contexts()
                 target_context = active['name'] if active else 'delhi'
             except Exception:
                 target_context = 'delhi'

        print(f"WS YAML using context: {target_context}")
        api_client_ctx = config.new_client_from_config(config_file=kubeconfig_file, context=target_context)
        dyn_client_ctx = dynamic.DynamicClient(api_client_ctx)
        
        while True:
            try:
                dr_api = dyn_client_ctx.resources.get(api_version=ISTIO_VERSION, group=ISTIO_GROUP, kind="DestinationRule")
                vs_api = dyn_client_ctx.resources.get(api_version=ISTIO_VERSION, group=ISTIO_GROUP, kind="VirtualService")
                
                dr = dr_api.get(name="whereami", namespace=NAMESPACE)
                vs = vs_api.get(name="whereami-vs", namespace=NAMESPACE)
                
                dr_dict = dr.to_dict()
                if 'metadata' in dr_dict:
                    if isinstance(dr_dict['metadata'], dict):
                        dr_dict['metadata'].pop('annotations', None)
                        dr_dict['metadata'].pop('managedFields', None)
                dr_yaml = yaml.dump(dr_dict)

                vs_dict = vs.to_dict()
                if 'metadata' in vs_dict:
                    if isinstance(vs_dict['metadata'], dict):
                        vs_dict['metadata'].pop('annotations', None)
                        vs_dict['metadata'].pop('managedFields', None)
                vs_yaml = yaml.dump(vs_dict)
                
                await websocket.send_json({
                    "dr": dr_yaml,
                    "vs": vs_yaml
                })
            except Exception as e:
                await websocket.send_json({"error": str(e)})
            await asyncio.sleep(3)
    except WebSocketDisconnect:
        print("Websocket YAML disconnected")


# Initialize Kubernetes Client
try:
    # Use in-cluster config when deployed to GKE
    config.load_incluster_config()
    print('--local config based connect--')
except config.ConfigException:
    # Fallback to local kubeconfig for development
    kubeconfig_file = os.getenv("KUBECONFIG")
    if not kubeconfig_file and os.path.exists("kdp_kube_config"):
        kubeconfig_file = "kdp_kube_config"
        
    if kubeconfig_file:
        config.load_kube_config(config_file=kubeconfig_file)
        print(f'--fallback to custom kubeconfig: {kubeconfig_file}--')
    else:
        config.load_kube_config()
        print('--fallback to default kubeconfig--')

# Verify connection by listing available contexts
try:
    contexts, active_context = config.list_kube_config_contexts()
    if contexts:
        print("Available contexts:")
        for context in contexts:
            print(f" - {context['name']}")
    else:
        print("Cannot find any contexts in kube-config file.")
except Exception as e:
    print(f"Could not list contexts (might be using in-cluster config or file missing): {e}")



dyn_client = dynamic.DynamicClient(api_client.ApiClient())

# Istio API Details
ISTIO_GROUP = "networking.istio.io"
ISTIO_VERSION = "v1beta1"
NAMESPACE = os.getenv("NAMESPACE", "default")

class TrafficConfig(BaseModel):
    weight_mumbai: int
    failover_enabled: bool
    outlier_detection_enabled: bool

class TrafficGenConfig(BaseModel):
    service_name: str
    rps: int
    duration: int
    feature_flag: str = None

# Global state for traffic generation
traffic_task = None
traffic_running = False

async def generate_traffic_loop(service_url: str, rps: int, duration: int, feature_flag: str = None):
    global traffic_running
    delay = 1.0 / rps if rps > 0 else 1.0
    start_time = time.time()
    
    while traffic_running:
        if time.time() - start_time >= duration:
            traffic_running = False
            break
            
        try:
            def fetch():
                req = urllib.request.Request(service_url)
                if feature_flag:
                    req.add_header('x-feature-flag', feature_flag)
                with urllib.request.urlopen(req, timeout=2.0) as response:
                    body = response.read().decode('utf-8')
                    try:
                        data = json.loads(body)
                        return f"JSON Response: {json.dumps(data)}"
                    except json.JSONDecodeError:
                        return f"Response: {body}"
                        
            msg = await asyncio.to_thread(fetch)
            if msg:
                print(msg)
                await broadcast_message(msg)
        except Exception as e:
            # We expect some errors if the service is down (e.g., 503) during our chaos test
            pass
        await asyncio.sleep(delay)

@app.get("/api/contexts")
async def get_contexts():
    try:
        contexts, active_context = config.list_kube_config_contexts()
        names = [c['name'] for c in contexts]
        active = active_context['name'] if active_context else ""
        return {"contexts": names, "active": active}
    except Exception as e:
        return {"contexts": [], "active": "", "error": str(e)}

@app.post("/api/generateTraffic")
async def start_traffic(cfg: TrafficGenConfig):
    global traffic_task, traffic_running
    
    # Prefix with http if missing
    url = cfg.service_name
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
        
    if traffic_running and traffic_task:
        traffic_running = False
        await asyncio.sleep(0.5) # Wait briefly for loop to terminate

    traffic_running = True
    print(f"Starting traffic generation for service: {url} at {cfg.rps} RPS for {cfg.duration}s")
    traffic_task = asyncio.create_task(generate_traffic_loop(url, cfg.rps, cfg.duration, cfg.feature_flag))
    return {"status": "success", "message": f"Traffic started to {url} at {cfg.rps} RPS for {cfg.duration}s"}

@app.post("/api/stopTraffic")
async def stop_traffic():
    global traffic_task, traffic_running
    traffic_running = False
    if traffic_task:
        traffic_task.cancel()
        traffic_task = None
    return {"status": "success", "message": "Traffic stopped"}

@app.get("/", response_class=HTMLResponse)
async def get_index():
    with open("index.html", "r") as f:
        return f.read()

@app.get("/api/destination-rules/distribute")
async def get_dr_distribute():
    try:
        dr_api = dyn_client.resources.get(api_version=ISTIO_VERSION, group=ISTIO_GROUP, kind="DestinationRule")
        drs = dr_api.get()
        distribute_settings = []
        for dr in drs.items:
            try:
                name = dr.metadata.name
                spec = dr.spec
                traffic_policy = getattr(spec, 'trafficPolicy', {}) if hasattr(spec, 'trafficPolicy') else spec.get('trafficPolicy', {})
                load_balancer = getattr(traffic_policy, 'loadBalancer', {}) if hasattr(traffic_policy, 'loadBalancer') else traffic_policy.get('loadBalancer', {})
                locality_lb = getattr(load_balancer, 'localityLbSetting', {}) if hasattr(load_balancer, 'localityLbSetting') else load_balancer.get('localityLbSetting', {})
                distribute = getattr(locality_lb, 'distribute', None) if hasattr(locality_lb, 'distribute') else locality_lb.get('distribute', None)
            except AttributeError:
                dr_dict = dr.to_dict() if hasattr(dr, 'to_dict') else dr
                spec = dr_dict.get('spec', {})
                distribute = spec.get('trafficPolicy', {}).get('loadBalancer', {}).get('localityLbSetting', {}).get('distribute', None)
            
            if distribute and isinstance(distribute, list):
                distribute_settings.extend(distribute)
        
        return {"distribute": distribute_settings}
    except Exception as e:
        return {"error": str(e), "distribute": []}

@app.post("/api/destination-rules/distribute")
async def update_dr_distribute(payload: dict):
    try:
        print(f"Received distribute payload: {payload}")
        dr_api = dyn_client.resources.get(api_version=ISTIO_VERSION, group=ISTIO_GROUP, kind="DestinationRule")
        
        mode = payload.get("mode")
        if mode == "simple_rr":
            dr_patch = {
                "spec": {
                    "trafficPolicy": {
                        "loadBalancer": {
                            "simple": "ROUND_ROBIN",
                            "localityLbSetting": {
                                "enabled": False
                            }
                        }
                    }
                }
            }
        else:
            distribute_data = payload.get("distribute", {})
            dr_patch = {
                "spec": {
                    "trafficPolicy": {
                        "loadBalancer": {
                            "localityLbSetting": {
                                "enabled": True,
                                "distribute": [
                                    {
                                        "from": "*",
                                        "to": distribute_data
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        
        errors = []
        contexts, _ = config.list_kube_config_contexts()
        for ctx in contexts:
            context_name = ctx['name']
            try:
                print(f"Applying DestinationRule patch to context: {context_name}")
                api_client_ctx = config.new_client_from_config(context=context_name)
                dyn_client_ctx = dynamic.DynamicClient(api_client_ctx)
                dr_api_ctx = dyn_client_ctx.resources.get(api_version=ISTIO_VERSION, group=ISTIO_GROUP, kind="DestinationRule")
                dr_api_ctx.patch(name="whereami", namespace=NAMESPACE, body=dr_patch, content_type="application/merge-patch+json")
            except Exception as patch_e:
                print(f"Error on context {context_name}: {patch_e}")
                errors.append(f"{context_name}: {patch_e}")
                
        if errors:
            return {"status": "partial_success", "message": f"Applied with errors in some contexts", "errors": errors}
            
        return {"status": "success", "message": "DestinationRule distribute policy applied to all contexts"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/update-traffic")
async def update_traffic(cfg: TrafficConfig):
    
    """
    Updates Istio VirtualService (Weights) and DestinationRule (Outlier/Failover).
    """
    try:
        
        # 1. Update VirtualService for Traffic Splitting
        vs_api = dyn_client.resources.get(api_version=ISTIO_VERSION, group=ISTIO_GROUP, kind="VirtualService")
        
        # Retrieve the kubernetes virtual service deployed in the cluster in yaml format
        current_vs = vs_api.get(name="whereami", namespace=NAMESPACE)
        current_vs_yaml = yaml.dump(current_vs.to_dict())
        print(f"Current VirtualService YAML:\n{current_vs_yaml}")
        
        # Patch the existing VirtualService named 'whereami'
        vs_patch = {
            "spec": {
                "http": [{
                    "route": [
                        {
                            "destination": {"host": "whereami", "subset": "delhi"},
                            "weight": 100 - cfg.weight_mumbai
                        },
                        {
                            "destination": {"host": "whereami", "subset": "mumbai"},
                            "weight": cfg.weight_mumbai
                        }
                    ]
                }]
            }
        }
        vs_api.patch(name="whereami", namespace=NAMESPACE, body=vs_patch, content_type="application/merge-patch+json")

        # 2. Update DestinationRule for Outlier Detection & Failover
        dr_api = dyn_client.resources.get(api_version=ISTIO_VERSION, group=ISTIO_GROUP, kind="DestinationRule")
        
        # Define Outlier Detection settings
        outlier_settings = None
        if cfg.outlier_detection_enabled:
            outlier_settings = {
                "consecutive5xxErrors": 3,
                "interval": "10s",
                "baseEjectionTime": "30s",
                "maxEjectionPercent": 100
            }

        dr_patch = {
            "spec": {
                "trafficPolicy": {
                    "outlierDetection": outlier_settings
                }
            }
        }
        
        # If failover is specifically managed via DestinationRule locality settings
        if cfg.failover_enabled:
            dr_patch["spec"]["trafficPolicy"]["loadBalancer"] = {
                "localityLbSetting": {
                    "enabled": True,
                    "failover": [{"from": "asia-south2", "to": "asia-south1"}]
                }
            }
        else:
            dr_patch["spec"]["trafficPolicy"]["loadBalancer"] = {"simple": "ROUND_ROBIN"}

        dr_api.patch(name="whereami", namespace=NAMESPACE, body=dr_patch, content_type="application/merge-patch+json")

        return {"status": "success", "message": "Istio policies updated"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)