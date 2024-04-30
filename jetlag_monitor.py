import base64
import cherrypy
import configparser
import json
import os
import paramiko
import requests
import subprocess
import yaml

from jinja2 import Environment, FileSystemLoader

# Setup Templating Environment
j2env = Environment(loader=FileSystemLoader('templates'))

# Read Ansible Inventory File
inventory = configparser.ConfigParser()
inventory.read('/root/jetlag/ansible/inventory/cloud09.local')

HOSTS = list(map(lambda h: h.split(' ')[0].split('.')[0], inventory['hv'])).copy()
VMS = {}
SSH_CONS = {}

for vm in inventory['hv_vm']:
    hv = inventory['hv_vm'][vm].split()[0].split('.')[0]
    vm = vm.split()[0]
    if hv not in VMS:
        VMS[hv] = []
    VMS[hv].append(vm)

# Setup Paramiko
class IgnoreMissingHostKey(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        pass

def get_ssh(hv, new=False):
    if not SSH_CONS.get(hv, None) or new:
        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(IgnoreMissingHostKey)
        ssh.connect(hostname=f'{hv}.rdu2.scalelab.redhat.com', username='root', timeout=5)
        SSH_CONS[hv] = ssh

    return SSH_CONS[hv]

# Commands to get HV data
props = {
        'vms': 'virsh list --all --name | grep -c vm',
        'vmsshtdwn': 'virsh list --all | grep -c shut',
        'vmsinavg': 'cat /opt/hv/vm* | grep "inbound average" -c',
        'hvsping': 'ping6 -c 2 fc00:1006::1 | grep -c "time=50"',
        'hvsnvme': 'df -h /mnt/disk2/ --output=pcent,size | tail -n1',
        #'hvsnvme': 'df -h /dev/nvme0n1p1 | grep nvme | awk \'{print $2}\'',
        }

tgts = ('clusterversion', 'nodes', 'clusteroperators', 'managedcluster', 'clusterimageset')
cmds = {
        'kubepass': 'cat /root/bm/kubeadmin-password',
        'manifests': 'ls /root/hv-vm/standard/manifests/ | wc -l',
    'runningtest': 'tail -n1 /root/acm-deploy-load/results/$(ls /root/acm-deploy-load/results/ | tail -n1)/monitor_data.csv',
    #'runningtest': 'tail -n1 /root/acm-deploy-load/results/*$(ps aux | grep acm-deploy-load.py | head -n1 | tr -s " " | cut -d \  -f 36)/monitor_data.csv',
}

res_map = {
    'Versions': None,
    'ACM Version': 'ACM',
    'AAP Version': 'AAP',
    'Hub OCP': 'Hub OCP',
    'Deployed OCP': 'Deployed OCP',
    'Argo ZTP': 'cluster(s) per ZTP argoCD application',
    'Batch': 'cluster(s) per 3600s interval',
    'Interval': 'cluster(s) per 3600s interval',
    'Iterations': 'Actual Intervals',
    'WAN Emu': 'Wan Emulation',
    'Deployed Clusters Results': None,
    'Available': 'Available Clusters',
    'Deployed': 'Deployed (Applied/Committed) Clusters',
    'Installed': 'Installed Clusters',
    'Failed': 'Failed Clusters',
    'Not Started': 'InstallationNotStarted Clusters',
    'Successful': 'Cluster Successful Percent',
    'DeployedFailed': {'t': 'Failed', 'm':'Cluster Failed Percent'},
    'Managed Clusters Results': None,
    'ManagedInstalled': {'t': 'Installed', 'm':'Installed Clusters'},
    'Managed': 'Managed Clusters',
    'Successful %': 'Managed Successful Percent',
    'Failed %': 'Managed Failed Percent',
    'DU Profile Results': None,
    'Initalized': 'DU Profile Initialized',
    'Compliant': 'DU Profile Compliant',
    'Timeout': 'DU Profile Timeout',
    'DUSuccessful': {'t': 'Successful', 'm': 'DU Profile Successful Percent'},
    'DUFailed': {'t': 'Failed', 'm': 'DU Profile Failed Percent'},
    'Overall Results': None,
    'OverSuccessful': {'t': 'Successful', 'm': 'Overall Success (DU Compliant / Deployed)'},
    'Success': 'Overall Success Percent',
    'OverFailed': {'t': 'Failed', 'm': 'Overall Failed Percent'},
}
test_map = [
    'Date:',
    'Applied/Committed Clusters:',
    'Initialized Clusters:',
    'Not Started Clusters:',
    'Booted Nodes:',
    'Discovered Nodes:',
    'Installing Clusters:',
    'Failed Clusters:',
    'Completed Clusters:',
    'Managed Clusters:',
    'Initialized Policy Clusters:',
    'Policy Not Started Clusters:',
    'Policy Applying Clusters:',
    'Policy Timedout Clusters:',
    'Policy Compliant Clusters:',
    'Playbook Running Clusters:',
    'Playbook Completed Clusters:'
]


# CherryPy Configs
cherrypy.config.update({'server.socket_port': 8082})
cherrypy.config.update({'server.socket_host': '0.0.0.0'})
cherrypy.config.update({'server.thread_pool': 100})

reg_post_process = {
    'ocp4/openshift4': lambda res: list(filter(lambda x: x.endswith('x86_64'), res)),
}


def get_registry_tags(repository):
    #if repository in registry_urls:
    headers = {'Authorization': 'Basic ' + base64.b64encode(b'registry:registry').decode()}
    #url = registry_urls[repository]
    url = f'https://localhost:5000/v2/{repository}'
    if repository == '_catalog':
        url += '?n=10000'
    else:
        url += '/tags/list'
    print(url)
    result = json.loads(requests.get(url, headers=headers, verify=False).content[:-1])
    result = result['repositories'] if repository == '_catalog' else result['tags']
    if repository in reg_post_process:
        result = reg_post_process[repository](result)
    return result


class AcmMonitor(object):
    @cherrypy.expose
    def index(self):
        hosts = HOSTS
        vms = VMS
        tmpl = j2env.get_template('index.html')
        return tmpl.render(hosts=hosts, vms=vms)

    @cherrypy.expose
    def registry(self, repository):
        namespace = ''
        if repository.startswith('_catalog'):
            x = repository.split('/')
            if '/' in repository:
                namespace = x[1]
            repository = x[0]
        res = get_registry_tags(repository)
        res.sort()
        if repository == '_catalog':
            if namespace:
                res = [i.split('/')[1] for i in res if i.startswith(namespace + '/')]
                res = list(map(lambda x: f'<a href="#" onclick="get_reg_tags(event, \'{namespace}/{x}\');">{x}</a>', res))
                res.sort()
            else:
                res = map(lambda x: x.split('/')[0], res)
                res = set(res)
                res = list(map(lambda x: f'<a href="#" onclick="get_reg_tags(event, \'_catalog/{x}\');">{x}</a>', res))
                res.sort()
        return '<div class="small">' + '<br>'.join(res) + '</div>'

    @cherrypy.expose
    def validations(self):
        with open("/root/jetlag/ansible/vars/all.yml") as jetlagvars:
            try:
                jetlagvars = yaml.safe_load(jetlagvars)
            except yaml.YAMLError as exc:
                return exc

        with open("/root/jetlag/ansible/vars/hv.yml") as hvvars:
            try:
                hvvars = yaml.safe_load(hvvars)
            except yaml.YAMLError as exc:
                return exc

        with open("/root/acm-deploy-load/ansible/vars/all.yml") as acmvars:
            try:
                acmvars = yaml.safe_load(acmvars)
            except yaml.YAMLError as exc:
                return exc

        acmtags = get_registry_tags('acm-d/acm-custom-registry')
        ocptags = get_registry_tags('ocp4/openshift4')
        ocptags = reg_post_process['ocp4/openshift4'](ocptags) # Filter out excess tags
        optags = get_registry_tags('olm-mirror/redhat-operator-index')
        ztptags = get_registry_tags('openshift-kni/ztp-site-generator')
        SUCCESS = 'success'
        DANGER = 'danger'
        #val_html = '''{}<p> {}<p> {}<p> {}<p> {} <p>
        val_html = '''<div class="row">
  <div class="col-4">
      jetlag/all.yml</b>
      <div class="small">
      Hub OCP Version: {}<br>
      ocp_release_image: {}<br>
      operator_index_tag: <span class='text-{} val'>{}<span class="valtext">{}</span></span><br>
      hv_vm_bandwidth_limit: {}
      </div><br>
      jetlag/hv.yml
      <div class="small">
      cluster_image_set: <span class="text-{} val">{}<span class="valtext">{}</span></span><br>
      site_config_overrides: {}
      </div>
  </div>
  <div class="col-8">
      acm-deploy-load/all.yml
      <div class="small">
      rhacm_build: <span class='text-{} val'>{}<span class="valtext">{}</span></span><br>
      du_profile_version: <span class='text-{} val'>{}<span class="valtext">{}</span></span><br>
      cnf_features_deploy_branch: <span class='text-{} val'>{}<span class="valtext">{}</span></span><br>
      ztp_site_generator_image_tag: <span class='text-{} val'>{}<span class="valtext">{}</span></span><br>
      operator_index_tag: <span class='text-{} val'>{}<span class="valtext">{}</span></span><br>
      mce_assisted_ocp_version: {}<br>
      mce_clusterimagesets<br>
       - name: <span class="text-{} val">{}<span class="valtext">{}</span></span><br>
       - releaseImage: {}<br>
      manyPolicies: {}<br>
      extraHubCommonTemplates: {}<br>
      extraHubGroupTemplates: {}<br>
       </div>
  </div>
</div>

        '''
        vals = {}
        valtext = {}
        vals['jl_op_idx_tag'] = jetlagvars['operator_index_tag'] == acmvars.get('operator_index_tag', '-')
        vals['jl_op_idx_tag_in_reg'] = jetlagvars['operator_index_tag'] in optags
        valtext['jl_op_idx_tag'] = '{} {} the acm_all Operator Index Tag<br>'.format(
                    jetlagvars['operator_index_tag'],
                    'matches' if vals['jl_op_idx_tag'] else 'does not match')
        valtext['jl_op_idx_tag'] += '{} is {} the bastion registry<br>'.format(
                    jetlagvars['operator_index_tag'],
                    'included' if vals['jl_op_idx_tag_in_reg'] else 'missing from')
        vals['hv_cluster_image_set_in_reg'] = [i for i in ocptags if i.startswith(hvvars['cluster_image_set'].split('-')[1])] 
        valtext['hv_cluster_image_set'] = '{} is {} the bastion registry<br>'.format(
                    hvvars['cluster_image_set'],
                    'included' if vals['hv_cluster_image_set_in_reg'] else 'missing from')
        vals['hv_cluster_image_set_eq_acm_all'] = hvvars['cluster_image_set'] == acmvars['mce_clusterimagesets'][0]['name'] 
        valtext['hv_cluster_image_set'] += '{} {} the acm_all imageset var'.format(
                    hvvars['cluster_image_set'],
                    'matches' if vals['hv_cluster_image_set_eq_acm_all'] else 'does not match')
        vals['rhacm_build_in_reg'] = acmvars['rhacm_build'] in acmtags
        valtext['rhacm_build'] = '{} is {} the bastion registry<br>'.format(
                    acmvars['rhacm_build'],
                    'included' if vals['rhacm_build_in_reg'] else 'missing from')
        vals['acm_all_cluster_image_set_in_reg'] = [i for i in ocptags if i.startswith(acmvars['mce_clusterimagesets'][0]['name'].split('-')[1])]
        valtext['acm_all_cluster_image_set'] = '{} is {} the bastion registry<br>'.format(
                    acmvars['mce_clusterimagesets'][0]['name'],
                    'included' if vals['acm_all_cluster_image_set_in_reg'] else 'missing from')
        valtext['acm_all_cluster_image_set'] += '{} {} the hv imageset var'.format(
                    acmvars['mce_clusterimagesets'][0]['name'],
                    'matches' if vals['hv_cluster_image_set_eq_acm_all'] else 'does not match')
        vals['du_profile_version_eq_cnf_features'] = str(acmvars.get('du_profile_version', '-')) in acmvars.get('cnf_features_deploy_branch', '-')
        vals['du_profile_version_eq_ztp_image'] = acmvars.get('ztp_site_generator_image_tag', '-').startswith('v{}'.format(acmvars.get('du_profile_version', '-')))
        valtext['du_profile_version'] = '{} {} the CFN Features branch version<br>'.format(
                    acmvars.get('du_profile_version', '-'),
                    'matches' if vals['du_profile_version_eq_cnf_features'] else 'does not match')
        valtext['du_profile_version'] += '{} {} the ZTP image verion<br>'.format(
                    acmvars.get('du_profile_version', '-'),
                    'matches' if vals['du_profile_version_eq_ztp_image'] else 'does not match')
        valtext['cnf_features'] = '{} {} the DU Profile version<br>'.format(
                    acmvars.get('cnf_features_deploy_branch', '-'),
                    'matches' if vals['du_profile_version_eq_cnf_features'] else 'does not match')
        vals['acm_op_idx_tag'] = jetlagvars['operator_index_tag'] == acmvars.get('operator_index_tag', '-')
        valtext['acm_op_idx_tag'] = '{} {} the jetlag_all Lag Operator Index Tag<br>'.format(
                    acmvars.get('operator_index_tag', '-'),
                    'matches' if vals['jl_op_idx_tag'] else 'does not match')
        vals['acm_op_idx_tag_in_reg'] = acmvars.get('operator_index_tag', '-') in optags
        valtext['acm_op_idx_tag'] += '{} is {} the bastion registry<br>'.format(
                    acmvars.get('operator_index_tag', '-'),
                    'included' if vals['acm_op_idx_tag_in_reg'] else 'missing from')
        vals['ztp_image_in_reg'] = acmvars.get('ztp_site_generator_image_tag', '-') in ztptags
        valtext['ztp_image_set'] = '{} is {} the bastion registry<br>'.format(
                    acmvars.get('ztp_site_generator_image_tag', '-'),
                    'included' if vals['ztp_image_in_reg'] else 'missing from')
        valtext['ztp_image_set'] += '{} {} the DU Profile version<br>'.format(
                    acmvars.get('ztp_site_generator_image_tag', '-'),
                    'matches' if vals['du_profile_version_eq_ztp_image'] else 'does not match')
        return val_html.format(#jetlagvars, hvvars, acmvars, acmtags, ocptags,
                   jetlagvars['openshift_version'],
                   jetlagvars['ocp_release_image'],
                   SUCCESS if vals['jl_op_idx_tag'] else DANGER,
                   jetlagvars['operator_index_tag'],
                   valtext['jl_op_idx_tag'],
                   jetlagvars['hv_vm_bandwidth_limit'],
                   SUCCESS if vals['hv_cluster_image_set_in_reg'] and vals['hv_cluster_image_set_eq_acm_all'] else DANGER,
                   hvvars['cluster_image_set'],
                   valtext['hv_cluster_image_set'],
                   hvvars.get('siteconfig_du_sno_install_config_overrides', '-'),
                   SUCCESS if vals['rhacm_build_in_reg'] else DANGER,
                   acmvars['rhacm_build'],
                   valtext['rhacm_build'],
                   SUCCESS if vals['du_profile_version_eq_cnf_features'] and vals['du_profile_version_eq_ztp_image'] else DANGER,
                   acmvars.get('du_profile_version', '-'),
                   valtext['du_profile_version'],
                   SUCCESS if vals['du_profile_version_eq_cnf_features'] else DANGER,
                   acmvars.get('cnf_features_deploy_branch', '-'),
                   valtext['cnf_features'],
                   SUCCESS if vals['ztp_image_in_reg'] else DANGER,
                   acmvars.get('ztp_site_generator_image_tag', '-'),
                   valtext['ztp_image_set'],
                   SUCCESS if vals['acm_op_idx_tag'] else DANGER,
                   acmvars.get('operator_index_tag', '-'),
                   valtext['acm_op_idx_tag'],
                   acmvars['mce_assisted_ocp_version'],
                   SUCCESS if vals['acm_all_cluster_image_set_in_reg'] and vals['hv_cluster_image_set_eq_acm_all'] else DANGER,
                   acmvars['mce_clusterimagesets'][0]['name'],
                   valtext['acm_all_cluster_image_set'],
                   acmvars['mce_clusterimagesets'][0]['releaseImage'],
                   acmvars.get('manyPolicies', '-'),
                   acmvars.get('extraHubCommonTemplates', '-'),
                   acmvars.get('extraHubGroupTemplates', '-'),
                   )

    @cherrypy.expose
    def results(self):
        # initialize compose
        compose = {}
        compose['iterations'] = []
        for i in res_map:
            compose[i] = [] if res_map[i] else None

        #base_dir = '/root/acm-deploy-load/results'
        base_dir = '/opt/http_store/data/results'
        res_list = sorted(os.listdir(base_dir))
        for rd in res_list:
            if '-' not in rd:
                continue 
            itr = rd.split('-')[-1]
            with open(os.path.join(base_dir,
                                   rd, 'report.txt'), 'r') as data:
                res_lines = data.readlines()

            res = {}
            for l in res_lines:
                if l.startswith(' * '):
                    if ':' in l:
                        val = l[:-1].split(' ')[-1]
                        if 'DOWNSTREAM' in val:
                            val = val.replace('DOWNSTREAM-', '')[:-9]
                        if 'aap-op' in val:
                            val = val.replace('aap-operator.v', '')
                        res[l[3:l.find(':')]] = val
                    else:
                        val = l[3:l.find(' ', 3)]
                        res[l[:-1][l.find(' ', 3)+1:]] = val
                        print(val, l[:-1][l.find(' ', 3)+1:])

            compose['iterations'].append(itr)
            for k,v in res_map.items():
                if v:
                    if type(v) == dict:
                        compose[k].append(res[v['m']] if v['m'] in res else '0')
                    else:
                        compose[k].append(res[v] if v in res else '0')



        tmpl = j2env.get_template('results.html')
        return tmpl.render(results=compose, res_map=res_map)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def lcl(self, cmd):
        if cmd in cmds:
            print(cmds[cmd])
            res = subprocess.run([cmds[cmd]],
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,
                                 shell=True)
            err = res.stderr.decode()
            out = res.stdout.decode() 
            print(out)
            if err:
                res = err
            elif cmd == 'runningtest':
                res = out.split(',')
                rez = list(zip(test_map, res))
                #res = list(map(lambda x: ' '.join(x), rez))
                #res = '\n'.join(res)
                print(rez)
                return {'headers': [], 'rows': rez, 'tgt': cmd, 'status': False }
            else:
                res = out
            return {'res': res}
        return {'res': 'x'}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def hv(self, hv, prop):
        if prop in props:
            ssh = get_ssh(hv)
            try: 
                ssh_stdin, ssh_stdout, ssh_stderr = ssh.exec_command(props[prop])
            except:
                ssh = get_ssh(hv, True)
                ssh_stdin, ssh_stdout, ssh_stderr = ssh.exec_command(props[prop])
            stdout = ssh_stdout.read().strip()
            stderr = ssh_stderr.read().strip()
            return {'res': stdout.decode() or stderr.decode()}
        return {'res': 'x'}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def oc(self, tgt):
        if tgt in tgts:
            result = subprocess.run([f"KUBECONFIG=/root/bm/kubeconfig oc get {tgt}"],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    shell=True)


            if result.stdout:
                res = result.stdout.decode().split('\n')
                headers = res[0].split()
                if tgt == 'managedcluster':
                    headers.remove('HUB')
                    headers.remove('MANAGED')
                    headers.remove('CLUSTER')
                    headers.remove('URLS')
                rows = []
                rows.append(headers)
                for row in res[1:]:
                    ct = len(headers)
                    _row = row.split()
                    if tgt == 'managedcluster' and _row:
                        if len(_row) > ct:
                            del _row[2]
                        elif len(_row) < ct:
                            while len(_row) < ct:
                                _row.insert(2, '')
                        rows.append(_row)
                    else:
                        if len(_row) > ct:
                            new = _row[0:ct]
                            new[-1] = ' '.join(_row[ct-1:])
                            rows.append(new)
                        elif _row:
                            rows.append(_row)
                print(rows)
                return {'headers': headers, 'rows': rows, 'tgt': tgt,
                        'status': 'STATUS' in headers or 'MESSAGE' in headers }

if __name__ == '__main__':
    cherrypy.quickstart(AcmMonitor())
