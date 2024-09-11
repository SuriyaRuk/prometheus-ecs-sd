#!/usr/bin/python
import argparse
import signal
import boto3
import logging
from aiohttp import web
import asyncio
import yaml
import sys
import json

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(prog='prometheus-ecs-sd', description='Prometheus file discovery for AWS ECS')
    parser.add_argument('-f', '--file', type=str, default='/tmp/ecs_file_sd.yml', help='File to write tasks (default: /tmp/ecs_file_sd.yml)')
    parser.add_argument('-c', '--cluster', type=str, default='', help='Return metrics only for this Cluster name (default: all)')
    parser.add_argument('-i', '--interval', type=int, default=60, help='Interval to discover ECS tasks, seconds (default: 60)')
    parser.add_argument('-l', '--log', choices=['debug', 'info', 'warn'], default='info', help='Logging level (default: info)')
    parser.add_argument('-p', '--port', type=int, default=8080, help='Port to serve /metrics (default: 8080)')
    parser.add_argument('-s', '--service', type=str, default='', help='Return metrics only for this Service  name (default: all)')
    args = parser.parse_args()
    logger.setLevel(getattr(logging, args.log.upper()))
    return args


class Discoverer:
    def __init__(self, file, cluster,service):
        self.file = file
        self.cluster = cluster
        self.service = service
        self.tasks = {}      # ecs tasks cache
        self.hosts = {}      # ec2 container instances cache
        try:
            self.ecs = boto3.client('ecs')
            self.ec2 = boto3.client('ec2')
            self.ecs.list_clusters()  # check creds on start
        except Exception as e:
            sys.exit(e)

    async def loop(self, interval):
        signal.signal(signal.SIGINT, self.signal_handler)
        el = asyncio.get_event_loop()
        i = 0
        while True:
            try:
                await asyncio.wait_for(el.run_in_executor(None, self.discover), timeout=interval)
            except asyncio.exceptions.TimeoutError:
                logger.error(f'Timeout while reading ECS Tasks! Try to increase --interval')
            except Exception:
                logger.error(f'Read tasks error:', exc_info=True)
            await asyncio.sleep(interval)
            i += 1
            # drop caches
            if i > 1440:
                i = 0
                self.tasks = {}

    def discover(self):
        targets = []
        tasks = 0
        for cluster in self.ecs.list_clusters().get('clusterArns', []):
            if self.cluster and cluster.split('/')[-1] != self.cluster:
                continue
            for page in self.ecs.get_paginator('list_tasks').paginate(cluster=cluster,serviceName=self.service):
                for arn in page.get('taskArns', []):
                    #print(arn)
                    targets += self.check_task(cluster=cluster, arn=arn)
                    tasks += 1
        logger.info(f"Discovered {len(targets)} targets from {tasks} tasks")
        with open(self.file, 'w') as f:
            yaml.dump(targets, f)

    def check_task(self, cluster, arn):
        if arn not in self.tasks:
            task = self.ecs.describe_tasks(cluster=cluster, tasks=[arn])['tasks'][0]
            td = self.ecs.describe_task_definition(taskDefinition=task['taskDefinitionArn'])['taskDefinition']
            #print(json.dumps(task['containers'], indent=4, default=str))
            if 'containers' not in task:  # not yet mapped, skip caching
                return []
            #ip = self.get_host_ip(cluster, task['containers'][0])
            ip = task['containers'][0]['networkInterfaces'][0]['privateIpv4Address']
            sd = []
            for container in td['containerDefinitions']:
                scrapes = container.get('dockerLabels', {}).get('PROMETHEUS_SCRAPES')
                if scrapes:
                    labels = self.get_labels(container.get('dockerLabels', {}).get('PROMETHEUS_LABELS'))
                    labels['container_name'] = container['name']
                    labels['task_name'] = td['family']
                    labels['task_revision'] = td['revision']
                    tc = [x for x in task['containers'] if x['name'] == container['name']][0]
                    labels['container_arn'] = tc.get('containerArn', '')
                    labels['__container_image'] = tc.get('image', '')
                    labels['__task_group'] = task.get('group', '')
                    labels['__container_runtime_id'] = tc.get('runtimeId', '')
                    #labels['instance_id'] = self.hosts[task['containerInstanceArn']]['id']
                    #ip = container['networkInterfaces'][0]['privateIpv4Address']
                    for port in scrapes.split(','):
                        tmp = labels.copy()
                        if '/' in port:
                            port, path = port.split('/', maxsplit=1)
                            tmp['__metrics_path__'] = f'/{path}'
                        port = self.get_mapped_port(int(port), container, task['containers'])
                        if port is None:  # not yet mapped, skip caching
                            return []
                        sd.append({
                            'targets': [f'{ip}:{port}'],
                            'labels': tmp
                        })
            self.tasks[arn] = sd
            logger.debug(f'Got task {arn} obj: {self.tasks[arn]}')
        return self.tasks[arn]

    def get_host_ip(self, cluster, arn):
        if arn not in self.hosts:
            self.hosts[arn] = {
                'ip': arn[0]['networkInterfaces'][0]['privateIpv4Address']
            }
            logger.debug(f'Got host {arn} IP: {self.hosts[arn]["ip"]}')
        return self.hosts[arn]["ip"]

    # "__scheme__=https,skip_15s=true" => {"__scheme__": "https", "skip_15s": "true"}
    @staticmethod
    def get_labels(str):
        if not str:
            return {}
        try:
            return dict(x.split('=', maxsplit=1) for x in str.split(','))
        except:
            logger.warning(f'Unable to parse Labels: {str}')

    # find host 'port' mapping of container 'definition' in running 'containers'
    @staticmethod
    def get_mapped_port(port, definition, containers):
        portmap = [x for x in definition.get('portMappings', {}) if x['containerPort']==port]
        if not portmap:
            return port  # hostNet
        if portmap[0]['hostPort'] == 0:  # dynamic host ports
            for container in containers:
                if container['name'] == definition['name']:
                    if 'networkBindings' not in container:
                        logger.info(f'Container {container["name"]} is not yet mapped to host port, skipping')
                        return None
                    for bind in container['networkBindings']:
                        if bind['containerPort'] == port:
                            return bind['hostPort']
        else:
            return portmap[0]['hostPort']  # mapped port

    @staticmethod
    def signal_handler(num, frame):
        sys.exit(0)


class Metrics:
    def __init__(self, cluster):
        self.cluster = cluster
        self.ecs = boto3.client('ecs')

    async def handler(self, request):
        res = ''
        for cluster in self.ecs.list_clusters().get('clusterArns', []):
            if self.cluster and cluster.split('/')[-1] != self.cluster:
                continue
            for page in self.ecs.get_paginator('list_services').paginate(cluster=cluster):
                for arn in page.get('serviceArns', []):
                    service = self.ecs.describe_services(cluster=cluster, services=[arn])['services'][0]
                    res += f'ecs_service_desired_tasks{{service="{service["serviceName"]}"}} {service["desiredCount"]}\n'
                    res += f'ecs_service_running_tasks{{service="{service["serviceName"]}"}} {service["runningCount"]}\n'
                    res += f'ecs_service_pending_tasks{{service="{service["serviceName"]}"}} {service["pendingCount"]}\n'
        return web.Response(text=res)


async def start_background_tasks(app):
    app['discovery'] = asyncio.create_task(Discoverer(app['args'].file, app['args'].cluster,app['args'].service).loop(app['args'].interval))


async def cleanup_background_tasks(app):
    app['discovery'].cancel()
    await app['discovery']

if __name__ == "__main__":
    args = parse_args()
    logger.debug(f"Starting with args: {args}")
    app = web.Application()
    app['args'] = args
    app.router.add_get("/metrics", Metrics(args.cluster).handler)
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    web.run_app(app, port=args.port, access_log=logger)
