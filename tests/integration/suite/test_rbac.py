import kubernetes
import pytest
from rancher import ApiError
import time

from .common import random_str
from .conftest import wait_until_available, \
    cluster_and_client, user_project_client, \
    kubernetes_api_client, wait_for, ClusterContext


def test_multi_user(admin_mc, user_mc):
    """Tests a bug in the python client where multiple clients would not
    work properly. All clients would get the auth header of the last  client"""
    # Original admin client should be able to get auth configs
    ac = admin_mc.client.list_auth_config()
    assert len(ac) > 0

    # User client should not. We currently dont 404 on this, which would be
    # more correct. Instead, list gets filtered to zero
    ac = user_mc.client.list_auth_config()
    assert len(ac) == 0


def test_project_owner(admin_cc, admin_mc, user_mc, remove_resource):
    """Tests that a non-admin member can create a project, create and
    add a namespace to it, and can do workload related things in the namespace.

    This is the first test written incorporating a non-admin user and the
    kubernetes python client. It does a lot partially as an experiment and
    partially as an example for other yet-to-be-written tests
    """
    admin_client = admin_mc.client
    admin_client.create_cluster_role_template_binding(
        userId=user_mc.user.id,
        roleTemplateId="cluster-member",
        clusterId=admin_cc.cluster.id,
    )

    user_client = user_mc.client

    # When this returns, the user can successfully access the cluster and thus
    # can create a project in it. We generally need this wait_until_available
    # call when we are creating cluster, project, and namespaces as non-admins
    # because until the rbac controllers have had a chance to run and the
    # creator is bootstrapped into the resource, they will not be able to
    # access it
    wait_until_available(user_client, admin_cc.cluster)

    proj_name = 'test-' + random_str()

    def can_create_project():
        try:
            p = user_client.create_project(name=proj_name,
                                           clusterId=admin_cc.cluster.id)
            # In case something goes badly as the user, add a finalizer to
            # delete the project as the admin
            remove_resource(p)
            return p
        except ApiError as e:
            assert e.error.status == 403
            return False

    proj = wait_for(can_create_project)

    # When this returns, the user can successfully access the project and thus
    # can create a namespace in it
    proj = wait_until_available(user_client, proj)
    proj = user_client.wait_success(proj)
    assert proj.state == 'active'

    k8s_client = kubernetes_api_client(user_client, 'local')
    auth = kubernetes.client.AuthorizationV1Api(k8s_client)

    # Rancher API doesn't have a surefire way of knowing exactly when the user
    # has the ability to create namespaces yet. So we have to rely on an actual
    # kubernetes auth check.
    def can_create_ns():
        access_review = kubernetes.client.V1SelfSubjectAccessReview(spec={
            "resourceAttributes": {
                'verb': 'create',
                'resource': 'namespaces',
                'group': '',
            },
        })
        response = auth.create_self_subject_access_review(access_review)
        return response.status.allowed

    wait_for(can_create_ns)

    c_client = cluster_and_client('local', user_mc.client)[1]
    ns = c_client.create_namespace(name='test-' + random_str(),
                                   projectId=proj.id)
    ns = wait_until_available(c_client, ns)
    ns = c_client.wait_success(ns)
    assert ns.state == 'active'

    # Simple proof that user can get pods in the created namespace.
    # We just care that the list call does not error out
    core = kubernetes.client.CoreV1Api(api_client=k8s_client)
    core.list_namespaced_pod(ns.name)

    # As the user, assert that the two expected role bindings exist in the
    # namespace for the user. There should be one for the rancher role
    # 'project-owner' and one for the k8s built-in role 'admin'
    rbac = kubernetes.client.RbacAuthorizationV1Api(api_client=k8s_client)
    rbs = rbac.list_namespaced_role_binding(ns.name)
    rb_dict = {}
    assert len(rbs.items) == 4
    for rb in rbs.items:
        if rb.subjects[0].name == user_mc.user.id:
            rb_dict[rb.role_ref.name] = rb
    assert 'project-owner' in rb_dict
    assert 'admin' in rb_dict

    # As an additional measure of proof and partially just as an exercise in
    # using this particular k8s api, check that the user can create
    # deployments using the subject access review api
    access_review = kubernetes.client.V1LocalSubjectAccessReview(spec={
        "resourceAttributes": {
            'namespace': ns.name,
            'verb': 'create',
            'resource': 'deployments',
            'group': 'extensions',
        },
    })
    response = auth.create_self_subject_access_review(access_review)
    assert response.status.allowed is True

    # List_namespaced_pod just list the pods of default core groups.
    # If you want to list the metrics of pods,
    # users should have the permissions of metrics.k8s.io group.
    # As a proof, we use this particular k8s api, check that the user can list
    # pods.metrics.k8s.io using the subject access review api
    access_review = kubernetes.client.V1LocalSubjectAccessReview(spec={
        "resourceAttributes": {
            'namespace': ns.name,
            'verb': 'list',
            'resource': 'pods',
            'group': 'metrics.k8s.io',
        },
    })
    response = auth.create_self_subject_access_review(access_review)
    assert response.status.allowed is True


def test_removing_user_from_cluster(admin_pc, admin_mc, user_mc, admin_cc,
                                    remove_resource):
    """Test that a user added to a project in a cluster is able to see that
    cluster and after being removed from the project they are no longer able
    to see the cluster.
    """

    # Yes, this is misspelled, it's how the actual label is spelled.
    mbo = 'memberhsip-binding-owner'

    admin_client = admin_mc.client
    prtb = admin_client.create_project_role_template_binding(
        userId=user_mc.user.id,
        roleTemplateId="project-member",
        projectId=admin_pc.project.id,
    )
    remove_resource(prtb)

    # Verify the user can see the cluster
    wait_until_available(user_mc.client, admin_cc.cluster)

    api_instance = kubernetes.client.RbacAuthorizationV1Api(
        admin_mc.k8s_client)

    # Find the expected k8s clusterRoleBinding
    crbs = api_instance.list_cluster_role_binding(
        label_selector=prtb.uuid + "=" + mbo)

    assert len(crbs.items) == 1

    # Delete the projectRoleTemplateBinding, this should cause the user to no
    # longer be able to see the cluster
    admin_mc.client.delete(prtb)

    def crb_callback():
        crbs = api_instance.list_cluster_role_binding(
            label_selector=prtb.uuid + "=" + mbo)
        return len(crbs.items) == 0

    def fail_handler():
        return "failed waiting for cluster role binding to be deleted"

    wait_for(crb_callback, fail_handler=fail_handler)

    try:
        cluster = user_mc.client.by_id_cluster(admin_cc.cluster.id)
        assert cluster is None
    except ApiError as e:
        assert e.error.status == 403


def test_user_role_permissions(admin_mc, user_factory, remove_resource):
    """Test that a standard user can only see themselves """
    admin_client = admin_mc.client

    # Create 4 new users, one with user-base
    user1 = user_factory()
    user2 = user_factory(globalRoleId='user-base')
    user_factory()
    user_factory()

    users = admin_client.list_user()
    # Admin should see at least 5 users
    assert len(users.data) >= 5

    # user1 should only see themselves in the user list
    users1 = user1.client.list_user()
    assert len(users1.data) == 1, "user should only see themselves"

    # user1 can see all roleTemplates
    role_templates = user1.client.list_role_template()
    assert len(role_templates.data) > 0, ("user should be able to see all " +
                                          "roleTemplates")

    # user2 should only see themselves in the user list
    users2 = user2.client.list_user()
    assert len(users2.data) == 1, "user should only see themselves"
    # user2 should not see any role templates
    role_templates = user2.client.list_role_template()
    assert len(role_templates.data) == 0, ("user2 does not have permission " +
                                           "to view roleTemplates")


def test_impersonation_passthrough(admin_mc, admin_cc, user_mc, user_factory,
                                   remove_resource, request):
    """Test users abalility to impersonate other users"""
    admin_client = admin_mc.client

    user1 = user_factory()
    user2 = user_factory()

    admin_client.create_cluster_role_template_binding(
        userId=user1.user.id,
        roleTemplateId="cluster-member",
        clusterId=admin_cc.cluster.id,
    )

    admin_client.create_cluster_role_template_binding(
        userId=user2.user.id,
        roleTemplateId="cluster-owner",
        clusterId=admin_cc.cluster.id,
    )

    wait_until_available(user1.client, admin_cc.cluster)
    wait_until_available(user2.client, admin_cc.cluster)

    admin_k8s_client = kubernetes_api_client(admin_client, 'local')
    user1_k8s_client = kubernetes_api_client(user1.client, 'local')
    user2_k8s_client = kubernetes_api_client(user2.client, 'local')

    admin_auth = kubernetes.client.AuthorizationV1Api(admin_k8s_client)
    user1_auth = kubernetes.client.AuthorizationV1Api(user1_k8s_client)
    user2_auth = kubernetes.client.AuthorizationV1Api(user2_k8s_client)

    access_review = kubernetes.client.V1SelfSubjectAccessReview(spec={
        "resourceAttributes": {
            'verb': 'impersonate',
            'resource': 'users',
            'group': '',
        },
    })

    # Admin can always impersonate
    response = admin_auth.create_self_subject_access_review(access_review)
    assert response.status.allowed is True

    # User1 is a member of the cluster which does not grant impersonate
    response = user1_auth.create_self_subject_access_review(access_review)
    assert response.status.allowed is False

    # User2 is an owner/admin which allows them to impersonate
    response = user2_auth.create_self_subject_access_review(access_review)
    assert response.status.allowed is True

    # Add a role and role binding to user user1 allowing user1 to impersonate
    # user2
    admin_rbac = kubernetes.client.RbacAuthorizationV1Api(admin_k8s_client)
    body = kubernetes.client.V1ClusterRole(
        metadata={'name': 'limited-impersonator'},
        rules=[{
            'resources': ['users'],
            'apiGroups': [''],
            'verbs': ['impersonate'],
            'resourceNames': [user2.user.id]
        }]
    )
    impersonate_role = admin_rbac.create_cluster_role(body)

    request.addfinalizer(lambda: admin_rbac.delete_cluster_role(
        impersonate_role.metadata.name))

    binding = kubernetes.client.V1ClusterRoleBinding(
        metadata={'name': 'limited-impersonator-binding'},
        role_ref={
            'apiGroups': [''],
            'kind': 'ClusterRole',
            'name': 'limited-impersonator'
        },
        subjects=[{'kind': 'User', 'name': user1.user.id}]
    )

    impersonate_role_binding = admin_rbac.create_cluster_role_binding(binding)

    request.addfinalizer(lambda: admin_rbac.delete_cluster_role_binding(
        impersonate_role_binding.metadata.name))

    access_review2 = kubernetes.client.V1SelfSubjectAccessReview(spec={
        "resourceAttributes": {
            'verb': 'impersonate',
            'resource': 'users',
            'group': '',
            'name': user2.user.id
        },
    })

    # User1 should now be abele to imerpsonate as user2
    response = user1_auth.create_self_subject_access_review(access_review2)
    assert response.status.allowed is True


def test_permissions_can_be_removed(admin_cc, admin_mc, user_mc, request,
                                    remove_resource, admin_pc_factory):
    def create_project_and_add_user():
        admin_pc_instance = admin_pc_factory()

        prtb = admin_mc.client.create_project_role_template_binding(
            userId=user_mc.user.id,
            roleTemplateId="project-member",
            projectId=admin_pc_instance.project.id,
        )
        remove_resource(prtb)
        return admin_pc_instance, prtb

    admin_pc1, _ = create_project_and_add_user()
    admin_pc2, prtb2 = create_project_and_add_user()

    def add_namespace_to_project(admin_pc):
        def safe_remove(client, resource):
            try:
                client.delete(resource)
            except ApiError:
                pass

        ns = admin_cc.client.create_namespace(name=random_str(),
                                              projectId=admin_pc.project.id)
        request.addfinalizer(lambda: safe_remove(admin_cc.client, ns))

        def ns_active():
            new_ns = admin_cc.client.reload(ns)
            return new_ns.state == 'active'

        wait_for(ns_active)

    add_namespace_to_project(admin_pc1)

    def new_user_cc(user_mc):
        cluster, client = cluster_and_client('local', user_mc.client)
        return ClusterContext(user_mc, cluster, client)

    user_cc = new_user_cc(user_mc)
    assert len(user_cc.client.list_namespace()) == 1

    add_namespace_to_project(admin_pc2)

    user_cc = new_user_cc(user_mc)
    assert len(user_cc.client.list_namespace()) == 2

    admin_mc.client.delete(prtb2)

    user_cc = new_user_cc(user_mc)
    wait_for(lambda: ns_count(user_cc.client, 1), timeout=30)


def ns_count(client, count):
    return len(client.list_namespace()) == count


def test_appropriate_users_can_see_kontainer_drivers(user_factory):
    kds = user_factory().client.list_kontainer_driver()
    assert len(kds) == 8

    kds = user_factory('clusters-create').client.list_kontainer_driver()
    assert len(kds) == 8

    kds = user_factory('kontainerdrivers-manage').client. \
        list_kontainer_driver()
    assert len(kds) == 8

    kds = user_factory('settings-manage').client.list_kontainer_driver()
    assert len(kds) == 0


def test_readonly_cannot_perform_app_action(admin_mc, admin_pc, user_factory,
                                            remove_resource):
    client = admin_pc.client
    project = admin_pc.project

    user = user_factory()
    remove_resource(user)
    ns = admin_pc.cluster.client.create_namespace(name=random_str(),
                                                  projectId=project.id)

    prtb = admin_mc.client.create_project_role_template_binding(
        name="prtb",
        userId=user.user.id,
        projectId=project.id,
        roleTemplateId="read-only")
    remove_resource(prtb)

    wait_until_available(user.client, project)

    app = client.create_app(
        name="testapp",
        externalId="catalog://?catalog=library&template=mysql&version=0.3.7&"
                   "namespace=cattle-global-data",
        targetNamespace=ns.name,
        projectId=project.id
    )

    wait_for_workload(client, ns.name, count=1)

    with pytest.raises(ApiError) as e:
        user.client.action(obj=app, action_name="upgrade",
                           answers={"abc": "123"})
    assert e.value.error.status == 403

    with pytest.raises(ApiError) as e:
        user.client.action(obj=app, action_name="rollback",
                           revisionId="test")
    assert e.value.error.status == 403


def test_member_can_perform_app_action(admin_mc, admin_pc, remove_resource,
                                       user_factory):
    client = admin_pc.client
    project = admin_pc.project

    user = user_factory()
    remove_resource(user)

    ns = admin_pc.cluster.client.create_namespace(name=random_str(),
                                                  projectId=project.id)

    prtb = admin_mc.client.create_project_role_template_binding(
        name="test-prtb",
        userId=user.user.id,
        projectId=project.id,
        roleTemplateId="project-owner")
    remove_resource(prtb)

    wait_until_available(user.client, project)

    app = client.create_app(
        name="testapp1",
        externalId="catalog://?catalog=library&template=mysql&version=0.3.7&"
                   "namespace=cattle-global-data",
        targetNamespace=ns.name,
        projectId=project.id
    )

    wait_for_workload(client, ns.name, count=1)

    user.client.action(
        obj=app,
        action_name="upgrade",
        answers={"asdf": "asdf"})
    '''
        TODO: write rollback test, currently blocked by issue #20204
    '''


def test_readonly_cannot_edit_secret(admin_mc, user_mc, admin_pc,
                                     remove_resource):
    """Tests that a user with readonly access is not able to create/update
     a secret or ns secret
    """
    project = admin_pc.project
    user_client = user_mc.client

    prtb = admin_mc.client.create_project_role_template_binding(
        name="prtb-" + random_str(),
        userId=user_mc.user.id,
        projectId=project.id,
        roleTemplateId="read-only"
    )
    remove_resource(prtb)

    wait_until_available(user_client, project)

    proj_user_client = user_project_client(user_mc, project)

    # readonly should failed to create a regular secret
    with pytest.raises(ApiError) as e:
        proj_user_client.create_secret(
            name="test-" + random_str(),
            stringData={
                'abc': '123'
             }
        )
    assert e.value.error.status == 403

    secret = admin_pc.client.create_secret(
        name="test-" + random_str(),
        stringData={
            'abc': '123'
        }
    )
    remove_resource(secret)

    wait_until_available(admin_pc.client, secret)

    # readonly should failed to update a regular secret
    with pytest.raises(ApiError) as e:
        proj_user_client.update_by_id_secret(
            id=secret.id,
            stringData={
                'asd': 'fgh'
            }
        )
    assert e.value.error.status == 404

    ns = admin_pc.cluster.client.create_namespace(
        name='test-' + random_str(),
        projectId=project.id
    )
    remove_resource(ns)

    # readonly should fail to create ns secret
    with pytest.raises(ApiError) as e:
        proj_user_client.create_namespaced_secret(
            namespaceId=ns.id,
            name="test-" + random_str(),
            stringData={
                'abc': '123'
            }
        )
    assert e.value.error.status == 403

    ns_secret = admin_pc.client.create_namespaced_secret(
        namespaceId=ns.id,
        name="test-" + random_str(),
        stringData={
            'abc': '123'
        }
    )
    remove_resource(ns_secret)

    wait_until_available(admin_pc.client, ns_secret)

    # readonly should fail to update ns secret
    with pytest.raises(ApiError) as e:
        proj_user_client.update_by_id_namespaced_secret(
            namespaceId=ns.id,
            id=ns_secret.id,
            stringData={
                'asd': 'fgh'
            }
        )
    assert e.value.error.status == 404


def test_member_can_edit_secret(admin_mc, admin_pc, remove_resource,
                                user_mc):
    """Tests that a user with project-member role is able to create/update
    secrets and namespaced secrets
    """
    project = admin_pc.project
    user_client = user_mc.client

    ns = admin_pc.cluster.client.create_namespace(
        name='test-' + random_str(),
        projectId=project.id
    )
    remove_resource(ns)

    prtb = admin_mc.client.create_project_role_template_binding(
        name="prtb-" + random_str(),
        userId=user_mc.user.id,
        projectId=project.id,
        roleTemplateId="project-member"
    )

    remove_resource(prtb)

    wait_until_available(user_client, project)

    proj_user_client = user_project_client(user_mc, project)

    def try_create_secret():
        try:
            return proj_user_client.create_secret(
                name="secret-" + random_str(),
                stringData={
                    'abc': '123'
                }
            )
        except ApiError as e:
            assert e.error.status == 403
        return False

    # Permission to create secret may not have been granted yet,
    # so it will be retried for 45 seconds
    secret = wait_for(try_create_secret, fail_handler=lambda:
                      "do not have permission to create secret")
    remove_resource(secret)

    wait_until_available(proj_user_client, secret)

    proj_user_client.update_by_id_secret(id=secret.id, stringData={
        'asd': 'fgh'
     })

    def try_create_ns_secret():
        try:
            return proj_user_client.create_namespaced_secret(
                name="secret-" + random_str(),
                namespaceId=ns.id,
                stringData={
                    "abc": "123"
                }
            )

        except ApiError as e:
            assert e.error.status == 403
        return False

    ns_secret = wait_for(try_create_ns_secret, fail_handler=lambda:
                         "do not have permission to create ns secret")
    remove_resource(ns_secret)

    wait_until_available(proj_user_client, ns_secret)

    proj_user_client.update_by_id_namespaced_secret(
        namespaceId=ns.id,
        id=ns_secret.id,
        stringData={
            "asd": "fgh"
        }
    )


def wait_for_workload(client, ns, timeout=60, count=0):
    start = time.time()
    interval = 0.5
    workloads = client.list_workload(namespaceId=ns)
    while len(workloads.data) != count:
        if time.time() - start > timeout:
            print(workloads)
            raise Exception('Timeout waiting for workload service')
        time.sleep(interval)
        interval *= 2
        workloads = client.list_workload(namespaceId=ns)
    return workloads
