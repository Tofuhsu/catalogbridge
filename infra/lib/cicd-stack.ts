import { Duration, Stack, StackProps, CfnOutput, Tags } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as iam from 'aws-cdk-lib/aws-iam';

export interface CicdProps extends StackProps {
  githubRepo: string; // owner/name
  githubBranch: string;
  /** `sub` prefix GitHub puts in the OIDC token. Repos using immutable subjects get
   *  `repo:<owner>@<ownerId>/<repo>@<repoId>`; check with
   *  `gh api repos/<owner>/<repo>/actions/oidc/customization/sub`. */
  githubSubjectPrefix?: string;
  existingOidcProviderArn?: string;
}

/**
 * GitHub Actions -> AWS via OIDC. No long-lived AWS keys are stored in GitHub.
 * The role can only be assumed by workflows of one repository on one branch, and
 * it can only assume the CDK bootstrap roles (which CDK uses to deploy).
 */
export class CicdStack extends Stack {
  constructor(scope: Construct, id: string, props: CicdProps) {
    super(scope, id, props);
    Tags.of(this).add('Project', 'catalogbridge');
    const provider = props.existingOidcProviderArn
      ? iam.OpenIdConnectProvider.fromOpenIdConnectProviderArn(this, 'GitHubOidc', props.existingOidcProviderArn)
      : new iam.OpenIdConnectProvider(this, 'GitHubOidc', {
        url: 'https://token.actions.githubusercontent.com',
        clientIds: ['sts.amazonaws.com'],
      });
    const role = new iam.Role(this, 'DeployRole', {
      roleName: 'catalogbridge-github-deploy',
      maxSessionDuration: Duration.hours(1),
      assumedBy: new iam.WebIdentityPrincipal(provider.openIdConnectProviderArn, {
        StringEquals: {
          'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
          'token.actions.githubusercontent.com:sub': `${props.githubSubjectPrefix ?? `repo:${props.githubRepo}`}:ref:refs/heads/${props.githubBranch}`,
        },
      }),
      description: 'Assumed by GitHub Actions (OIDC) to run cdk deploy for CatalogBridge',
    });
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['sts:AssumeRole'],
      resources: [`arn:aws:iam::${this.account}:role/cdk-hnb659fds-*-${this.account}-${this.region}`],
    }));
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['cloudformation:DescribeStacks'],
      resources: [`arn:aws:cloudformation:${this.region}:${this.account}:stack/CatalogBridge*/*`],
    }));
    new CfnOutput(this, 'DeployRoleArn', { value: role.roleArn });
  }
}
