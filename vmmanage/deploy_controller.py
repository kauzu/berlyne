# Berlyne IT security trainings platform
# Copyright (C) 2016 Ruben Gonzalez <rg@ht11.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
from glob import glob
from http.client import (
    OK as HTTP_OK,
    CONFLICT as HTTP_CONFLICT,
    NOT_FOUND as HTTP_NOT_FOUND
)
from os import path

from django.conf import settings
from django.db import IntegrityError

from uptomate import Deployment
from uptomate.Deployment import (
    INSTANCE_DIR_NAME,
    INSTALLED_MARKER_FILE
)
from .models import (
    vagr_factory,
    FLAG_FILE_NAME,
    LEGAL_API_VM_ACTIONS
)
from . import models
from . import tasks

__AVAIL_VAGR_FILES = []


def get_avail_vagrant_files():
    if not __AVAIL_VAGR_FILES:
        for p in glob(path.join(settings.VAGR_VAGRANT_PATH, '*')):
            if path.isfile(p):
                __AVAIL_VAGR_FILES.append(path.split(p)[-1])
    return __AVAIL_VAGR_FILES


def task_dict_success(task):
    return {
        'task_id': task.pk
    }, HTTP_OK


def install_deployment(vagr_depl: Deployment.Vagrant, vm):
    if isinstance(vm, str):
        vm = models.VirtualMachine.objects.get(slug=vm)

    try:
        config = vm.set_align_config(vagr_depl.get_config())
    except KeyError as ex:
        raise ValueError("Problem config is missing '{}' field".format(str(ex)))

    vagr_depl.set_ports(config['ports'])

    with vagr_depl.open_content_file(FLAG_FILE_NAME) as f:
        f.write(config['flag'])


def _install_deployment_callback(vagr_depl, f, vm_db, **kwargs):
    install_deployment(vagr_depl, vm_db)


def create_deployment(vm_slug, vagrant_name):

    if isinstance(vm_slug, models.VirtualMachine):
        vm_slug = vm_slug.slug

    vagr = vagr_factory(vm_slug)
    if vagr.installed:
        return "VM with that name already exists in fs", HTTP_CONFLICT

    try:
        # Set name as work around, so unique contraint is not violated
        # in case two VMs get created at the same time
        vm = models.VirtualMachine.objects.create(slug=vm_slug,
                                                  name=vm_slug)
    except IntegrityError:
        return "VM exists already in db", HTTP_CONFLICT

    t = tasks.run_on_vagr(
        vagr,
        'install',
        vm,
        _install_deployment_callback,
        vagrant_file_path=path.join(settings.VAGR_VAGRANT_PATH, vagrant_name)
    )

    vm.add_task(t, 'install')
    return task_dict_success(t)


def destroy_deployment(vm):
    vagr = vagr_factory(vm.slug)
    return task_dict_success(tasks.destroy_deployment(vagr, vm))


def destroy_deployment_db(vm):
    tasks.destroy_vm_db(vm)
    return "Deleted from db", HTTP_OK


def destroy_deployment_fs(vm_slug):
    return task_dict_success(models.Task.from_slug(
        'destroy',
        vm_slug)
    )


def _task_from_slug(action, vm_slug, vm_db=None, **kwargs):
    vagr = vagr_factory(vm_slug)
    return tasks.run_on_vagr(vagr, action, vm_db, **kwargs)


def run_on_existing(action, vm_obj, **kwargs):
    if action not in LEGAL_API_VM_ACTIONS:
        return "Action is not defined", HTTP_NOT_FOUND
    t = _task_from_slug(action, vm_obj.slug, vm_obj, **kwargs)
    vm_obj.add_task(t, action)
    return task_dict_success(t)


# Todo: make cheaper
def find_installable_problems():
    problems = []
    for task_path in glob(
            path.join(
                settings.VAGR_DEPLOYMENT_PATH,
                "*",
                Deployment.CONFIG_FILE_NAME
            )
    ):
        task_path = path.dirname(task_path)
        task_name = path.split(task_path)[-1]
        vagr = vagr_factory(task_name)
        if not vagr.installed and not models.VirtualMachine.objects.filter(
                slug=task_name).exists():
            problems.append(task_name)
    return problems


def action_on_state(vms, action, states, **action_kwargs):
    for vm in vms:
        predicted_state = vm.predict_state()
        if predicted_state == Deployment.VAGRANT_UNKNOWN or \
                        predicted_state in states:
            run_on_existing(action, vm, **action_kwargs)


def vm_action_on_states(action, states, vms=None):
    """
    Iterate over VMs and apply an action to the ones, that are
    PREDICTED to be in one of the given states after finishing
    their TaskQueue.
    This is done asynchronously.
    :param vms: VMs to iterate over, if None, all VMs will be used instead
    :param action: action to apply
    :param states: states the VM should be in to apply action.
    :return: task object
    """
    if vms is None:
        vms = models.VirtualMachine.objects.all()
    action_on_state(vms, action, states)