"""terraform.py: the module generated from a recommendation (F4).

Two kinds of test here, and the second kind is the point.

The offline ones check the mapping: that a Multi-AZ meter becomes a Multi-AZ
database, that the sizes in the plan are the sizes in the HCL, that a service
this cannot write is named as a gap rather than guessed at, and that two
services with the same name do not produce two blocks with the same label.

Then `terraform` itself is run, where it is installed. `fmt -check` proves the
generated text is formatted the way Terraform formats it, and `init` plus
`validate` check every resource and every attribute against the AWS provider's
own schema -- which is the only thing that can prove the module is real HCL for
real resources rather than something that merely looks like it. Those are marked
`network`, because `init` downloads a provider.

Wrong infrastructure-as-code is worse than none, so the bar for this file is that
a change to terraform.py which would produce a module nobody can apply cannot
pass without going red.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

import terraform
from schema import Recommendation
from tests.samples import FULL_JSON, RECOMMENDATION, usage

# `init` fetches a provider and `validate` walks a large schema, so the whole
# thing is given a generous ceiling rather than a tight one.
TERRAFORM_TIMEOUT = 300


@pytest.fixture
def recommendation() -> Recommendation:
    return Recommendation.model_validate_json(FULL_JSON)


def build(
    services: list[dict], region: str = "eu-west-2", headline: str = "A shape"
) -> Recommendation:
    """A recommendation holding only the services a test cares about."""
    return Recommendation.model_validate(
        {
            **RECOMMENDATION,
            "headline": headline,
            "region": region,
            "services": services,
        }
    )


def service(name: str, usage_lines: list[dict], purpose: str = "Does a thing") -> dict:
    return {"name": name, "purpose": purpose, "reasoning": "Because.", "usage": usage_lines}


def module(recommendation: Recommendation, **kwargs) -> dict[str, str]:
    """The generated module as {filename: text}."""
    return {written.name: written.text for written in terraform.files(recommendation, **kwargs)}


def written(recommendation: Recommendation, **kwargs) -> str:
    """Every .tf file's text, joined, for asserting something is somewhere."""
    files = module(recommendation, **kwargs)
    return "\n".join(text for name, text in files.items() if name.endswith(".tf"))


# --------------------------------------------------------------------------- #
# What the module is made of
# --------------------------------------------------------------------------- #


def test_a_recommendation_becomes_a_readable_module(recommendation):
    files = module(recommendation, project="Northbridge Mutual")

    assert set(files) == {
        "README.md",
        "providers.tf",
        "variables.tf",
        "network.tf",
        "security.tf",
        "main.tf",
        "outputs.tf",
        "terraform.tfvars.example",
    }
    for name, text in files.items():
        assert text.endswith("\n"), f"{name} does not end in a newline"
        assert "\t" not in text, f"{name} has a tab in it"
        for number, line in enumerate(text.split("\n"), start=1):
            # Any trailing whitespace, not only two spaces: a blank comment line
            # inside a header banner is the one that got away.
            assert line == line.rstrip(), f"{name}:{number} has trailing whitespace"


def test_every_file_says_it_is_a_starting_point(recommendation):
    """The one thing a reader must not be able to miss.

    The warning is wrapped across comment lines, so it is read here the way a
    person reads it rather than the way it is stored.
    """
    files = module(recommendation)

    def prose(text: str) -> str:
        return " ".join(text.replace("#", " ").split())

    assert "starting point, not a deployment" in prose(files["main.tf"])
    assert "starting point, not a deployment" in prose(files["providers.tf"])
    assert "**This is a starting point, not a deployment.**" in files["README.md"]


def test_the_region_is_the_one_the_architecture_was_priced_in():
    for region in ("eu-west-2", "us-east-1", "ap-southeast-2"):
        recommendation = build(
            [service("S3", [usage("s3-storage", quantity=10, hours=0)])], region=region
        )
        files = module(recommendation)
        assert f'default     = "{region}"' in files["variables.tf"]
        assert f'aws_region  = "{region}"' in files["terraform.tfvars.example"]


def test_the_provider_is_pinned_to_a_major_not_to_nothing(recommendation):
    providers = module(recommendation)["providers.tf"]

    assert 'source  = "hashicorp/aws"' in providers
    assert f'version = "{terraform.AWS_PROVIDER}"' in providers
    assert terraform.AWS_PROVIDER.startswith("~>")


def test_state_is_local_and_says_where_it_should_go_instead(recommendation):
    providers = module(recommendation)["providers.tf"]

    # Commented rather than configured: a generated backend pointing at a bucket
    # nobody owns is worse than none.
    assert '# backend "s3" {' in providers
    for line in providers.splitlines():
        if "backend" in line:
            assert line.lstrip().startswith("#"), line


# --------------------------------------------------------------------------- #
# The mapping from meters to resources
# --------------------------------------------------------------------------- #


def test_the_sizes_in_the_module_are_the_sizes_the_estimate_was_priced_on(recommendation):
    """The report's number and this module have to describe one architecture."""
    body = written(recommendation)

    for size in ("m6i.large", "db.m6g.large", "cache.m6g.large"):
        assert f'"{size}"' in body


def test_a_multi_az_meter_becomes_a_multi_az_database():
    recommendation = build(
        [
            service(
                "RDS MySQL",
                [usage("rds-instance-multi-az", size="db.m6g.large", variant="MySQL")],
            )
        ]
    )
    body = written(recommendation)

    assert "multi_az = true" in body
    assert 'engine         = "mysql"' in body


def test_a_single_az_meter_does_not():
    recommendation = build(
        [service("RDS MySQL", [usage("rds-instance", size="db.m6g.large", variant="MySQL")])]
    )

    assert "multi_az = false" in written(recommendation)


@pytest.mark.parametrize(
    "variant,engine,port",
    [
        ("PostgreSQL", "postgres", 5432),
        ("MySQL", "mysql", 3306),
        ("MariaDB", "mariadb", 3306),
        ("Oracle", "oracle-se2", 1521),
        ("SQL Server", "sqlserver-se", 1433),
        ("", "postgres", 5432),
    ],
)
def test_an_engine_is_written_in_the_name_the_provider_takes(variant, engine, port):
    """ "PostgreSQL" is for a human and for the price list. Terraform wants "postgres".

    The tier that connects to it is in the plan too, because the engine's port is
    only ever written as a rule from somewhere: a database nothing talks to has no
    port to open.
    """
    recommendation = build(
        [
            service("App", [usage("ec2-instance", size="m6i.large", quantity=2)]),
            service("Database", [usage("rds-instance", size="db.m6g.large", variant=variant)]),
        ]
    )
    body = written(recommendation)

    assert f'engine         = "{engine}"' in body
    assert f"from_port                    = {port}" in body


def test_only_the_engines_that_take_an_initial_database_get_one():
    """SQL Server takes no db_name, and setting one is a create that fails."""
    for variant in ("MySQL", "PostgreSQL"):
        recommendation = build(
            [service("Database", [usage("rds-instance", size="db.m6g.large", variant=variant)])]
        )
        assert "db_name  = var.db_name" in written(recommendation)

    recommendation = build(
        [service("Database", [usage("rds-instance", size="db.m6g.large", variant="SQL Server")])]
    )
    body = written(recommendation)
    assert "db_name  = var.db_name" not in body
    assert "takes no initial database name" in body


def test_a_password_is_never_written_anywhere():
    recommendation = build(
        [service("Database", [usage("rds-instance", size="db.m6g.large", variant="MySQL")])]
    )
    files = module(recommendation)
    body = "\n".join(files.values())

    assert "manage_master_user_password = true" in body
    # Not `password = `, not a variable for one, not a random_password resource.
    assert "password =" not in body.replace("manage_master_user_password =", "")
    assert "random_password" not in body


def test_redis_is_a_replication_group_and_memcached_is_a_cluster():
    redis = build(
        [
            service(
                "Cache",
                [usage("elasticache-node", size="cache.m6g.large", variant="Redis", quantity=2)],
            )
        ]
    )
    body = written(redis)
    assert 'resource "aws_elasticache_replication_group"' in body
    assert "num_cache_clusters   = 2" in body
    # Two nodes is a node to fail over to, so both flags are on.
    assert "automatic_failover_enabled = true" in body
    assert "multi_az_enabled           = true" in body

    memcached = build(
        [
            service(
                "Cache",
                [
                    usage(
                        "elasticache-node",
                        size="cache.m6g.large",
                        variant="Memcached",
                        quantity=3,
                    )
                ],
            )
        ]
    )
    body = written(memcached)
    assert 'resource "aws_elasticache_cluster"' in body
    assert "num_cache_nodes    = 3" in body
    assert 'az_mode            = "cross-az"' in body


def test_one_node_is_not_given_a_failover_it_has_nowhere_to_go():
    recommendation = build(
        [service("Cache", [usage("elasticache-node", size="cache.t4g.micro", variant="Redis")])]
    )
    body = written(recommendation)

    assert "automatic_failover_enabled = false" in body
    assert "multi_az_enabled           = false" in body


def test_a_graviton_size_gets_an_arm_image_and_an_intel_one_does_not():
    arm = build([service("App", [usage("ec2-instance", size="m6g.large", quantity=1)])])
    assert 'data "aws_ami" "al2023_arm64"' in written(arm)
    assert 'data "aws_ami" "al2023_x86_64"' not in written(arm)

    intel = build([service("App", [usage("ec2-instance", size="m6i.large", quantity=1)])])
    assert 'data "aws_ami" "al2023_x86_64"' in written(intel)
    assert 'data "aws_ami" "al2023_arm64"' not in written(intel)


@pytest.mark.parametrize(
    "size,arm",
    [
        ("m6g.large", True),
        ("c7gn.xlarge", True),
        ("t4g.small", True),
        ("r8g.medium", True),
        ("m6i.large", False),
        ("m6idn.xlarge", False),
        ("t3.micro", False),
        ("db.r6g.large", True),
        ("cache.t4g.micro", True),
        ("db.m5.large", False),
    ],
)
def test_graviton_is_read_off_the_family_not_guessed(size, arm):
    assert terraform.is_graviton(size) is arm


def test_the_ebs_a_service_was_priced_for_becomes_the_root_volume():
    """300 GB across three instances is 100 GB each, which is how it is billed."""
    recommendation = build(
        [
            service(
                "App",
                [
                    usage("ec2-instance", size="m6i.large", quantity=3),
                    usage("ebs-storage", quantity=300, hours=0),
                ],
            )
        ]
    )

    assert "volume_size           = 100" in written(recommendation)


def test_a_root_volume_is_never_smaller_than_the_image_needs():
    recommendation = build(
        [
            service(
                "App",
                [
                    usage("ec2-instance", size="m6i.large", quantity=4),
                    usage("ebs-storage", quantity=8, hours=0),
                ],
            )
        ]
    )

    assert "volume_size           = 20" in written(recommendation)


def test_a_tier_priced_for_part_of_the_day_scales_from_one():
    recommendation = build(
        [service("App", [usage("ec2-instance", size="m6i.large", quantity=4, hours=400.0)])]
    )
    body = written(recommendation)

    assert "min_size                  = 1" in body
    assert "desired_capacity          = 4" in body
    assert "sized this tier for part of the day" in body


def test_a_tier_priced_for_all_of_it_does_not():
    recommendation = build([service("App", [usage("ec2-instance", size="m6i.large", quantity=4)])])
    body = written(recommendation)

    assert "min_size                  = 4" in body
    assert "sized this tier for part of the day" not in body


def test_instances_go_behind_the_load_balancer(recommendation):
    body = written(recommendation)

    assert 'health_check_type         = "ELB"' in body
    assert "target_group_arns         = [aws_lb_target_group." in body


def test_instances_with_nothing_in_front_of_them_check_their_own_health():
    recommendation = build(
        [service("Batch workers", [usage("ec2-instance", size="m6i.large", quantity=2)])]
    )
    body = written(recommendation)

    assert 'health_check_type         = "EC2"' in body
    assert "target_group_arns" not in body
    # And nothing can reach them, which is said rather than left to be noticed.
    assert "Nothing in this plan sends traffic to the instances" in body


def test_a_bucket_is_private_versioned_and_encrypted():
    recommendation = build([service("Assets", [usage("s3-storage", quantity=500, hours=0)])])
    body = written(recommendation)

    for resource in (
        "aws_s3_bucket_public_access_block",
        "aws_s3_bucket_versioning",
        "aws_s3_bucket_server_side_encryption_configuration",
        "aws_s3_bucket_ownership_controls",
        "aws_s3_bucket_lifecycle_configuration",
    ):
        assert f'resource "{resource}"' in body
    assert "restrict_public_buckets = true" in body
    # A prefix, because a bucket name is global and a fixed one collides.
    assert "bucket_prefix = " in body
    assert "\n  bucket        = " not in body


def test_a_function_travels_with_a_handler_that_runs():
    recommendation = build(
        [
            service(
                "Lambda (orders)",
                [
                    usage("lambda-requests", quantity=5_000_000, hours=0),
                    usage("lambda-duration", quantity=250_000, hours=0),
                ],
            )
        ]
    )
    files = module(recommendation)

    handlers = [name for name in files if name.endswith("handler.py")]
    assert len(handlers) == 1
    assert "def handler(event, context):" in files[handlers[0]]

    body = written(recommendation)
    assert 'data "archive_file"' in body
    assert 'handler          = "handler.handler"' in body
    # The archive provider is only required where there is an archive to make.
    assert "hashicorp/archive" in files["providers.tf"]

    plain = build([service("S3", [usage("s3-storage", quantity=1, hours=0)])])
    assert "hashicorp/archive" not in module(plain)["providers.tf"]


def test_the_handler_directory_matches_what_the_archive_zips():
    recommendation = build(
        [service("Report generator", [usage("lambda-requests", quantity=100, hours=0)])]
    )
    files = module(recommendation)

    handler = next(name for name in files if name.endswith("handler.py"))
    folder = handler.rsplit("/", 1)[0].removeprefix("src/")
    assert f'source_dir  = "${{path.module}}/src/{folder}"' in files["main.tf"]


# --------------------------------------------------------------------------- #
# Read replicas, which are a guess and are treated as one
# --------------------------------------------------------------------------- #


def test_a_service_named_like_a_replica_becomes_one(recommendation):
    """The sample has a Multi-AZ primary and a read replica beside it."""
    body = written(recommendation)

    assert "replicate_source_db          = aws_db_instance." in body
    assert "Read from the name of the service" in body


def test_a_replica_of_a_different_engine_is_not_a_replica():
    """An Oracle standby is not a copy of a SQL Server instance."""
    recommendation = build(
        [
            service(
                "RDS SQL Server",
                [usage("rds-instance-multi-az", size="db.m5.large", variant="SQL Server")],
            ),
            service(
                "Oracle reporting standby",
                [usage("rds-instance", size="db.m5.large", variant="Oracle")],
            ),
        ]
    )
    body = written(recommendation)

    assert "replicate_source_db" not in body
    assert body.count('resource "aws_db_instance"') == 2


def test_an_engine_that_does_not_take_a_read_replica_is_not_given_one():
    recommendation = build(
        [
            service(
                "RDS SQL Server",
                [usage("rds-instance-multi-az", size="db.m5.large", variant="SQL Server")],
            ),
            service(
                "RDS SQL Server replica",
                [usage("rds-instance", size="db.m5.large", variant="SQL Server")],
            ),
        ]
    )

    assert "replicate_source_db" not in written(recommendation)


def test_a_replica_is_not_given_the_settings_it_inherits():
    """A replica with a username or a db_name is a create RDS refuses."""
    recommendation = build(
        [
            service(
                "RDS MySQL", [usage("rds-instance-multi-az", size="db.m6g.large", variant="MySQL")]
            ),
            service(
                "RDS MySQL read replica",
                [usage("rds-instance", size="db.m6g.large", variant="MySQL")],
            ),
        ]
    )
    files = module(recommendation)
    replica = files["main.tf"].split('resource "aws_db_instance"')[-1]

    assert "replicate_source_db" in replica
    for inherited in (
        "username",
        "db_name",
        "engine ",
        "allocated_storage",
        "db_subnet_group_name",
    ):
        assert inherited not in replica


def test_the_readme_says_which_database_was_read_as_a_copy(recommendation):
    readme = module(recommendation)["README.md"]

    assert "read replica of" in readme


# --------------------------------------------------------------------------- #
# What it will not write
# --------------------------------------------------------------------------- #


def test_a_service_nothing_can_size_is_named_rather_than_invented(recommendation):
    """CloudFront is in the sample, priced by nothing, and generated as nothing."""
    files = module(recommendation)

    assert "CloudFront" in files["README.md"]
    assert "## What it does not create" in files["README.md"]
    assert "CloudFront" in files["main.tf"]
    assert "aws_cloudfront" not in files["main.tf"]


def test_a_recommendation_of_nothing_writeable_still_leaves_a_vpc():
    recommendation = build(
        [
            service("CloudFront", [usage("unpriced", quantity=0, hours=0)]),
            service("Route 53", []),
        ]
    )
    files = module(recommendation)

    assert "Nothing in this recommendation maps to a resource" in files["main.tf"]
    assert 'resource "aws_vpc" "this"' in files["network.tf"]
    # No security groups, because there is nothing to put in one.
    assert "security.tf" not in files
    for name in ("CloudFront", "Route 53"):
        assert name in files["README.md"]


def test_ebs_with_no_instance_under_it_is_a_gap_not_a_dangling_volume():
    recommendation = build(
        [service("Shared scratch disk", [usage("ebs-storage", quantity=500, hours=0)])]
    )
    files = module(recommendation)

    assert "aws_ebs_volume" not in files["main.tf"]
    assert "EBS is written as the root volume of an instance" in files["README.md"]
    # Named once, not once for each of the two ways it could be described.
    assert files["README.md"].count("Shared scratch disk") == 1


def test_ebs_beside_a_tier_is_folded_into_it_rather_than_reported_missing():
    recommendation = build(
        [
            service("App", [usage("ec2-instance", size="m6i.large", quantity=2)]),
            service("Shared disk", [usage("ebs-storage", quantity=200, hours=0)]),
        ]
    )
    files = module(recommendation)

    assert "volume_size           = 120" in files["main.tf"]
    assert "What it does not create" not in files["README.md"]


def test_a_second_load_balancer_is_not_given_the_first_one_s_instances():
    recommendation = build(
        [
            service("Public ALB", [usage("application-load-balancer")]),
            service("Internal ALB", [usage("application-load-balancer")]),
            service("App", [usage("ec2-instance", size="m6i.large", quantity=2)]),
        ]
    )
    body = written(recommendation)

    assert body.count('resource "aws_lb" "') == 2
    # One target group has the group's instances; the other says it has none.
    assert body.count("target_group_arns         = [aws_lb_target_group.") == 1
    assert "Nothing is registered behind Internal ALB" in body


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #


def test_two_services_with_one_name_do_not_produce_one_label():
    recommendation = build(
        [
            service(
                "RDS MySQL", [usage("rds-instance-multi-az", size="db.m6g.large", variant="MySQL")]
            ),
            service("RDS MySQL!", [usage("rds-instance", size="db.m6g.large", variant="MySQL")]),
            service("RDS MySQL?", [usage("rds-instance", size="db.m6g.large", variant="MySQL")]),
        ]
    )
    body = written(recommendation)

    for label in ("rds_mysql", "rds_mysql_2", "rds_mysql_3"):
        assert f'resource "aws_db_instance" "{label}" {{' in body


def test_a_label_and_an_aws_name_do_not_collide_with_each_other():
    """They are separate namespaces: `aws_s3_bucket.s3` named `prefix-s3` is fine."""
    recommendation = build([service("S3", [usage("s3-storage", quantity=5, hours=0)])])
    body = written(recommendation)

    assert 'resource "aws_s3_bucket" "s3" {' in body
    assert 'bucket_prefix = "${var.name_prefix}-s3-"' in body


def test_a_name_is_shortened_at_a_word_rather_than_in_the_middle_of_one():
    recommendation = build(
        [
            service(
                "ElastiCache Redis cluster",
                [usage("elasticache-node", size="cache.m6g.large", variant="Redis")],
            )
        ]
    )

    assert 'replication_group_id = "${var.name_prefix}-elasticache"' in written(recommendation)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ElastiCache Redis", "elasticache"),
        ("Application Load Balancer", "application"),
        ("EC2 + Auto Scaling", "ec2-auto"),
        ("RDS MySQL (Multi-AZ)", "rds-mysql"),
        ("S3", "s3"),
        ("CloudFront", "cloudfront"),
        ("2 buckets", "buckets"),
        ("", "app"),
        ("!!!", "app"),
        ("averyverylongsinglewordindeed", "averyverylongs"),
    ],
)
def test_a_slug_is_short_lower_case_and_starts_with_a_letter(text, expected):
    result = terraform.slug(text)

    assert result == expected
    assert len(result) <= terraform.SLUG_LIMIT
    assert result[0].isalpha()
    assert "--" not in result
    assert not result.endswith("-")


def test_a_target_group_name_fits_in_the_thirty_two_characters_aws_allows():
    """The longest name the module builds, and the reason the limits are what they are."""
    longest = terraform.PREFIX_LIMIT + 1 + terraform.SLUG_LIMIT + len("-tg")

    assert longest <= 32


def test_a_prefix_short_enough_to_fail_the_module_s_own_validation_is_padded():
    assert terraform.prefix_for("Q") == "q-app"
    assert terraform.prefix_for("") == "advisor"
    assert terraform.prefix_for("Northbridge Mutual") == "northbridge"


def test_the_prefix_the_caller_asks_for_is_the_one_used():
    recommendation = build([service("S3", [usage("s3-storage", quantity=1, hours=0)])])

    assert 'default     = "acme-a"' in module(recommendation, prefix="acme-a")["variables.tf"]


# --------------------------------------------------------------------------- #
# Security defaults, which are decided here rather than by the model
# --------------------------------------------------------------------------- #


def test_instances_take_imds_v2_only(recommendation):
    assert 'http_tokens                 = "required"' in written(recommendation)


def test_there_is_no_key_pair_and_no_bastion(recommendation):
    body = written(recommendation)

    assert "key_name" not in body
    assert "AmazonSSMManagedInstanceCore" in body
    # Nothing accepts SSH from anywhere.
    assert "from_port                    = 22" not in body
    assert "from_port         = 22" not in body


def test_nothing_in_the_data_tier_can_reach_the_internet(recommendation):
    network = module(recommendation)["network.tf"]

    assert 'resource "aws_route_table" "data"' in network
    # The data route table gets no default route; the private ones do.
    data = network.split('resource "aws_route_table" "data"')[1]
    assert "nat_gateway_id" not in data


def test_a_database_is_not_reachable_from_outside_the_vpc(recommendation):
    body = written(recommendation)

    assert "publicly_accessible    = false" in body
    # Every rule into a database names a security group, never an address.
    for chunk in body.split('resource "aws_vpc_security_group_ingress_rule"')[1:]:
        rule = chunk.split("\n}")[0]
        if "aws_security_group.alb.id" in rule and "cidr_ipv4" in rule:
            continue  # the load balancer, which is the one thing facing outwards
        assert "cidr_ipv4" not in rule


def test_only_the_load_balancer_takes_traffic_from_an_address(recommendation):
    security = module(recommendation)["security.tf"]

    assert "for_each = toset(var.ingress_cidrs)" in security
    assert security.count("cidr_ipv4         = each.value") == 2  # 80, and 443 with a cert


def test_storage_is_encrypted_wherever_that_is_a_flag(recommendation):
    body = written(recommendation)

    assert "storage_encrypted     = true" in body  # RDS
    assert "encrypted             = true" in body  # EBS
    assert "at_rest_encryption_enabled = true" in body  # ElastiCache
    assert "transit_encryption_enabled = true" in body


def test_the_network_says_what_talked_to_what(recommendation):
    network = module(recommendation)["network.tf"]

    assert 'resource "aws_flow_log" "vpc"' in network
    assert 'resource "aws_iam_role" "flow_logs"' in network


# --------------------------------------------------------------------------- #
# The README, which is what makes it a starting point rather than a black box
# --------------------------------------------------------------------------- #


def test_the_readme_lists_what_to_fix_before_this_goes_anywhere(recommendation):
    readme = module(recommendation)["README.md"]

    assert "## Before this goes anywhere real" in readme
    for fix in ("Narrow the ingress", "Terminate TLS", "Pin the engine versions", "Move the state"):
        assert fix in readme


def test_the_readme_carries_the_notes_the_review_flagged(recommendation):
    """The gaps the advisor found, in the module that does not fix them."""
    readme = module(recommendation)["README.md"]

    assert "## What the review flagged" in readme
    assert "The load balancer should terminate HTTPS" in readme
    # Only the ones marked for review; what is already handled is not a to-do.
    assert "Multi-AZ RDS removes the database as a single point of failure" not in readme


def test_the_readme_names_every_resource_it_created(recommendation):
    readme = module(recommendation)["README.md"]

    for resource in (
        "aws_autoscaling_group",
        "aws_lb",
        "aws_db_instance",
        "aws_elasticache_replication_group",
        "aws_s3_bucket",
    ):
        assert resource in readme


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #


def test_what_comes_out_is_what_the_next_stack_needs(recommendation):
    outputs = module(recommendation)["outputs.tf"]

    assert 'output "vpc_id"' in outputs
    assert "dns_name" in outputs
    assert "_endpoint" in outputs
    assert "_secret_arn" in outputs


def test_no_output_carries_a_secret_in_it(recommendation):
    outputs = module(recommendation)["outputs.tf"]

    # The name of the secret, never its value.
    assert "master_user_secret[0].secret_arn" in outputs
    assert "master_password" not in outputs


def test_a_replica_has_no_secret_of_its_own(recommendation):
    """It uses the primary's, so an output for one would be a null.

    `try()` is what keeps the primary's own output from failing where RDS has not
    filled the list in yet; a replica has no such attribute at all.
    """
    outputs = module(recommendation)["outputs.tf"]

    assert outputs.count("_secret_arn") == 1


# --------------------------------------------------------------------------- #
# The formatter, reimplemented here because Terraform may not be installed
# --------------------------------------------------------------------------- #


def test_align_lines_up_a_run_of_assignments():
    text = 'resource "x" "y" {\n  a = 1\n  bbb = 2\n  cc = 3\n}\n'

    assert terraform.align(text) == ('resource "x" "y" {\n  a   = 1\n  bbb = 2\n  cc  = 3\n}\n')


def test_align_stops_at_a_blank_line_a_comment_and_a_nested_block():
    text = "block {\n  a = 1\n\n  bbb = 2\n  # a note\n  cc = 3\n  inner {\n    dddd = 4\n  }\n}\n"

    assert terraform.align(text) == text


def test_align_does_not_reach_across_indents():
    text = "block {\n  a = 1\n  inner {\n    dddd = 4\n    e = 5\n  }\n}\n"

    assert terraform.align(text) == (
        "block {\n  a = 1\n  inner {\n    dddd = 4\n    e    = 5\n  }\n}\n"
    )


def test_a_quoted_string_escapes_what_hcl_needs_escaped():
    assert terraform.quoted('a "quoted" name') == '"a \\"quoted\\" name"'
    assert terraform.quoted("a\\path") == '"a\\\\path"'


# --------------------------------------------------------------------------- #
# Terraform itself
#
# The only tests that can prove the module is real. Marked `network` because
# `init` fetches the AWS provider, and skipped where there is no terraform.
# --------------------------------------------------------------------------- #

SHAPES = {
    "sample": RECOMMENDATION,
    # S1. A model writing "$5/month", or a client called `${file("/etc/passwd")}`,
    # against the one thing that can actually prove the escaping is right: the
    # HCL parser. `fmt -check` reads it and `validate` resolves it, so if quoted()
    # ever stops defusing an interpolation this shape is what goes red.
    "hostile-text": [
        service(
            'Cache ${var.name_prefix} "tier" %{ if true }x%{ endif } | pipe',
            [usage("elasticache-node", size="cache.t4g.micro", variant="Redis")],
            purpose="Costs $5/month, 100% of the time",
        ),
        service(
            "RDS ${aws_db_instance.other.id}",
            [usage("rds-instance", size="db.t4g.micro", variant="PostgreSQL")],
        ),
    ],
    "web-tier": [
        service("Application Load Balancer", [usage("application-load-balancer")]),
        service(
            "EC2 web tier",
            [
                usage("ec2-instance", size="c7g.xlarge", quantity=4, hours=400.0),
                usage("ebs-storage", quantity=400, hours=0),
            ],
        ),
        service(
            "ElastiCache Memcached",
            [usage("elasticache-node", size="cache.m7g.large", variant="Memcached", quantity=3)],
        ),
        service(
            "RDS PostgreSQL", [usage("rds-instance", size="db.r6g.2xlarge", variant="PostgreSQL")]
        ),
    ],
    "serverless": [
        service("API Gateway", [usage("unpriced", quantity=0, hours=0)]),
        service(
            "Lambda (orders)",
            [
                usage("lambda-requests", quantity=5_000_000, hours=0),
                usage("lambda-duration", quantity=250_000, hours=0),
            ],
        ),
        service("S3 data lake", [usage("s3-storage", quantity=2000, hours=0)]),
    ],
    "licensed-databases": [
        service(
            "RDS SQL Server",
            [usage("rds-instance-multi-az", size="db.m5.large", variant="SQL Server")],
        ),
        service("RDS Oracle", [usage("rds-instance", size="db.m5.large", variant="Oracle")]),
    ],
    "nothing-writeable": [
        service("CloudFront", [usage("unpriced", quantity=0, hours=0)]),
        service("Route 53", []),
    ],
}


def terraform_binary() -> str | None:
    return shutil.which("terraform") or shutil.which("tofu")


def write_module(shape, directory) -> None:
    recommendation = (
        Recommendation.model_validate(shape)
        if isinstance(shape, dict)
        else build(shape, headline="A shape to check")
    )
    for file in terraform.files(recommendation, project="Northbridge Mutual"):
        path = directory / file.name
        path.parent.mkdir(parents=True, exist_ok=True)
        # Newlines held as written: `terraform fmt` on Windows would otherwise be
        # comparing against a file the test rewrote with \r\n.
        path.write_text(file.text, encoding="utf-8", newline="\n")


def run(binary: str, arguments: list[str], where) -> subprocess.CompletedProcess:
    environment = {
        **os.environ,
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "CHECKPOINT_DISABLE": "1",
        "NO_COLOR": "1",
    }
    return subprocess.run(
        [binary, *arguments],
        cwd=where,
        capture_output=True,
        text=True,
        timeout=TERRAFORM_TIMEOUT,
        env=environment,
        check=False,
    )


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_terraform_fmt_would_not_change_a_line_of_it(name, tmp_path):
    """Formatted the way Terraform formats it, without Terraform having to run.

    Not marked `network`: `fmt` reads files and downloads nothing, so this runs
    in the default suite wherever the binary is installed.
    """
    binary = terraform_binary()
    if not binary:
        pytest.skip("no terraform to format with")

    write_module(SHAPES[name], tmp_path)
    finished = run(binary, ["fmt", "-check", "-diff", "-recursive"], tmp_path)

    assert finished.returncode == 0, finished.stdout + finished.stderr


@pytest.mark.network
@pytest.mark.parametrize("name", sorted(SHAPES))
def test_terraform_validates_it_against_the_provider_s_own_schema(name, tmp_path):
    """Every resource and every attribute, checked by the AWS provider itself.

    This is the test that makes the module trustworthy: `validate` resolves each
    resource type and each argument against the schema the provider publishes, so
    an attribute that was renamed or removed in a major version cannot survive.
    """
    binary = terraform_binary()
    if not binary:
        pytest.skip("no terraform to validate with")

    write_module(SHAPES[name], tmp_path)

    initialised = run(binary, ["init", "-backend=false"], tmp_path)
    if initialised.returncode != 0:
        pytest.skip(f"terraform init could not reach the registry: {initialised.stderr[-300:]}")

    finished = run(binary, ["validate", "-json"], tmp_path)
    report = json.loads(finished.stdout or "{}")

    assert report.get("valid") is True, json.dumps(report.get("diagnostics", []), indent=2)
    assert report.get("error_count") == 0
    assert report.get("warning_count") == 0, json.dumps(report.get("diagnostics", []), indent=2)


# --------------------------------------------------------------------------- #
# Text that came from the model, written into a file that has to parse (S1, Q4)
#
# Everything in a generated module that is not boilerplate came out of a language
# model or out of a dialog box, and three of the formats it lands in have their
# own idea of what a character means: an HCL quoted string interpolates `${`, a
# Markdown table ends a row at a newline and a column at a `|`, and a Python
# docstring ends at `"""`. These are the tests that say so.
# --------------------------------------------------------------------------- #

HOSTILE = 'Cache ${var.name_prefix} "tier" %{ if true }x%{ endif } | pipe'


def evaluated(text: str) -> list[str]:
    """Every interpolation and directive an HCL parser would actually evaluate.

    A doubled sigil is HCL's escape, so `$${x}` is the four characters and not a
    reference to `x`. Those come out first -- which is why a plain "is the raw
    string absent" assertion cannot work here: `$${x}` contains `${x}` as a
    substring, so the escaped form would fail the very check that is meant to
    prove it.
    """
    live = text.replace("$${", "\x00").replace("%%{", "\x00")
    return re.findall(r"[$%]\{([^}]*)\}", live)


def test_an_interpolation_in_model_text_is_written_as_text():
    """S1. `${` and `%{` are HCL syntax, and quoted() has to defuse both.

    The escaping used to cover the backslash and the double quote only, so a
    service name or a client name carrying `${...}` reached the module as live
    code: Terraform evaluates it on plan, and `${file("/etc/passwd")}` in a
    client name put the contents of a local file into a resource tag. The far
    more common outcome was a model writing "$5/month" and producing a module
    that would not parse at all.
    """
    assert terraform.quoted('${file("/etc/passwd")}') == '"$${file(\\"/etc/passwd\\")}"'
    assert terraform.quoted("%{ if x }") == '"%%{ if x }"'
    # Doubling the sigil is what makes it literal, so nothing that survives can
    # open an interpolation.
    for hostile in ("${x}", "%{x}", "$${x}", "a ${b} c"):
        assert "${" not in terraform.quoted(hostile).replace("$${", "")
        assert "%{" not in terraform.quoted(hostile).replace("%%{", "")


def test_a_newline_in_model_prose_stays_on_one_line():
    """An HCL quoted string is single-line, and model prose occasionally is not."""
    assert terraform.quoted("one\ntwo\ttabbed") == r'"one\ntwo\ttabbed"'


def test_a_backslash_is_escaped_before_the_escapes_are_added():
    """Order matters: the other way round, the escapes escape each other."""
    assert terraform.quoted("back" + "\\" + "slash") == r'"back\\slash"'
    # A trailing backslash must not be able to escape the closing quote.
    assert terraform.quoted("ends with " + "\\") == r'"ends with \\"'


def test_a_hostile_service_name_reaches_every_file_as_text():
    """The whole module, not just the helper: nothing may carry it through raw.

    Asserted as "the escaped form is there and the raw form is not", rather than
    by looking for stray `${`. The module writes plenty of its own
    interpolations -- `${var.name_prefix}-vpc`, the flow-log ARN -- and once the
    model's copy has been defused to `$${var.name_prefix}` the two are only told
    apart by the doubled sigil in front of it. Which is the point.
    """
    recommendation = build(
        [service(HOSTILE, [usage("elasticache-node", "cache.t4g.micro", "Redis")])],
        headline=HOSTILE,
    )
    files = module(recommendation, project=f"Acme {HOSTILE}")
    generated = {
        name: text
        for name, text in files.items()
        if name.endswith(".tf") or name.endswith(".tfvars.example")
    }
    assert generated

    # Characterising every valid HCL expression is the wrong way round -- the
    # module writes real ones like `count.index + 1`. What matters is the other
    # direction: nothing the parser would evaluate came out of the model. A
    # double quote and the words below are in the hostile name and in none of the
    # expressions this module writes.
    for name, text in generated.items():
        for expression in evaluated(text):
            assert '"' not in expression, (name, expression)
            for word in ("tier", "if true", "endif", "pipe", "passwd"):
                assert word not in expression, (name, expression)

    written_text = "\n".join(generated.values())
    # And the escaped forms are there, so the text went through quoted() rather
    # than having been dropped somewhere on the way.
    assert "$${var.name_prefix}" in written_text
    assert "%%{ if true }" in written_text


def test_a_pipe_in_model_text_does_not_add_a_column_to_the_readme():
    """Q4. A `|` in a service name used to start a column the header never had."""
    recommendation = build(
        [service("RDS | reporting", [usage("rds-instance", "db.t4g.micro", "PostgreSQL")])]
    )
    readme = module(recommendation)["README.md"]

    assert r"RDS \| reporting" in readme
    # Every row in the resource table has exactly the three cells its header does.
    for line in readme.split("\n"):
        if line.startswith("| `aws_"):
            assert len(line.replace(r"\|", "").split("|")) == 5, line


def test_a_newline_in_model_text_does_not_end_a_readme_row_early():
    recommendation = build(
        [
            service(
                "Aurora",
                [usage("rds-instance", "db.t4g.micro", "PostgreSQL")],
                purpose="Holds orders.\nAnd customers.",
            )
        ]
    )
    plan = terraform.plan_for(recommendation)
    plan.unmapped.append(
        terraform.Unmapped("CloudFront", "Edge cache.\nTwo lines.", "Because.", "not written here")
    )
    readme = terraform.readme_md(plan)

    # Collapsed onto one line rather than ending the row where the newline was.
    # The gap table is where a purpose is printed; a mapped service shows its
    # shape instead, so CloudFront is the one that exercises this.
    assert "Edge cache. Two lines." in readme
    for line in readme.split("\n"):
        if line.startswith("| ") and line.endswith(" |"):
            assert len(line.replace(r"\|", "").split("|")) >= 4, line


def test_a_quote_in_a_service_name_cannot_close_the_handler_docstring():
    """Q4. The name reaches a Python docstring, so `\"\"\"` would end it early."""
    recommendation = build(
        [
            service(
                'Ingest """ pipeline',
                [usage("lambda-requests", quantity=1_000_000.0, hours=0.0)],
            )
        ]
    )
    handler = next(
        text
        for name, text in module(recommendation).items()
        if name.startswith("src/") and name.endswith("handler.py")
    )

    assert handler[:3] == '"""'
    # Exactly two triple quotes in the module docstring, and the name is inside it.
    assert handler.count('"""') == 4  # the module docstring and the handler's own
    compile(handler, "handler.py", "exec")
