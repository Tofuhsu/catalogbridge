import { Duration } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as cw from 'aws-cdk-lib/aws-cloudwatch';
import * as cwActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as apigw from 'aws-cdk-lib/aws-apigatewayv2';

export interface MonitoringProps {
  topic: sns.ITopic;
  functions: Record<string, lambda.Function>;
  ingestQueue: sqs.Queue;
  workQueue: sqs.Queue;
  dlqs: Record<string, sqs.Queue>;
  httpApi: apigw.HttpApi;
}

const NS = 'CatalogBridge';

export class Monitoring extends Construct {
  constructor(scope: Construct, id: string, props: MonitoringProps) {
    super(scope, id);
    const action = new cwActions.SnsAction(props.topic);
    const alarm = (aid: string, metric: cw.IMetric, threshold: number, description: string,
      op = cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD) => {
      const a = new cw.Alarm(this, aid, {
        metric, threshold, evaluationPeriods: 1, comparisonOperator: op,
        treatMissingData: cw.TreatMissingData.NOT_BREACHING, alarmDescription: description,
      });
      a.addAlarmAction(action);
      a.addOkAction(action);
      return a;
    };
    const app = (name: string, stat = 'Sum', service?: string) => new cw.Metric({
      namespace: NS, metricName: name, statistic: stat, period: Duration.minutes(1),
      dimensionsMap: service ? { Service: service } : undefined,
    });

    // A message in any DLQ means work needs an operator. It never means "done".
    for (const [name, q] of Object.entries(props.dlqs)) {
      alarm(`${name}NotEmpty`, q.metricApproximateNumberOfMessagesVisible({ period: Duration.minutes(1), statistic: 'Maximum' }), 1,
        `${name} has messages: inspect, fix the cause, then redrive with scripts/recover.py`);
    }
    alarm('WorkQueueOldestAge', props.workQueue.metricApproximateAgeOfOldestMessage({ period: Duration.minutes(1), statistic: 'Maximum' }),
      300, 'Work backlog older than 5 minutes (consumer too slow or stuck)');
    alarm('IngestQueueOldestAge', props.ingestQueue.metricApproximateAgeOfOldestMessage({ period: Duration.minutes(1), statistic: 'Maximum' }),
      300, 'Ingest backlog older than 5 minutes');
    for (const [name, f] of Object.entries(props.functions)) {
      alarm(`${name}Errors`, f.metricErrors({ period: Duration.minutes(5) }), 1, `${name} Lambda invocation errors`);
    }
    alarm('WorkerThrottles', props.functions.Worker.metricThrottles({ period: Duration.minutes(5) }), 1, 'Worker throttled');
    // Partial batch failures do not show up as Lambda Errors; alarm on the app metric too.
    alarm('RowSystemFailures', app('RowSystemFailures', 'Sum', 'WorkerFn'), 5, 'Rows failing with system errors (partial batch failures)');
    alarm('StalledJobs', app('StalledJobs', 'Maximum', 'StallCheckFn'), 1, 'Jobs with no progress for 10+ minutes');
    alarm('Api5xx', props.httpApi.metricServerError({ period: Duration.minutes(5) }), 5, 'API 5xx responses');

    const fnWidget = (title: string, fnMetric: (f: lambda.Function) => cw.IMetric) => new cw.GraphWidget({
      title, width: 12, left: Object.values(props.functions).map(fnMetric),
    });
    const dash = new cw.Dashboard(this, 'Dashboard', { dashboardName: 'CatalogBridge' });
    dash.addWidgets(
      new cw.GraphWidget({
        title: 'Queue depth (visible)', width: 12,
        left: [props.ingestQueue.metricApproximateNumberOfMessagesVisible(), props.workQueue.metricApproximateNumberOfMessagesVisible()],
        right: [props.workQueue.metricApproximateNumberOfMessagesNotVisible({ label: 'Work in flight' })],
      }),
      new cw.GraphWidget({
        title: 'Oldest message age (s)', width: 12,
        left: [props.ingestQueue.metricApproximateAgeOfOldestMessage(), props.workQueue.metricApproximateAgeOfOldestMessage()],
      }),
      new cw.GraphWidget({
        title: 'DLQ depth', width: 12,
        left: Object.values(props.dlqs).map(q => q.metricApproximateNumberOfMessagesVisible({ statistic: 'Maximum' })),
      }),
      new cw.GraphWidget({
        title: 'Rows: valid / invalid / duplicates skipped / system failures', width: 12,
        left: [app('RowsValid', 'Sum', 'WorkerFn'), app('RowsInvalid', 'Sum', 'WorkerFn'),
          app('DuplicatesSkipped', 'Sum', 'WorkerFn'), app('RowSystemFailures', 'Sum', 'WorkerFn')],
        right: [app('TxConflictRetries', 'Sum', 'WorkerFn')],
      }),
      fnWidget('Lambda errors', f => f.metricErrors()),
      fnWidget('Lambda throttles', f => f.metricThrottles()),
      fnWidget('Lambda duration p95', f => f.metricDuration({ statistic: 'p95' })),
      new cw.GraphWidget({
        title: 'Concurrent executions', width: 12,
        left: Object.values(props.functions).map(f => f.metric('ConcurrentExecutions', { statistic: 'Maximum' })),
      }),
      new cw.GraphWidget({
        title: 'Job end-to-end time (ms)', width: 12,
        left: [app('JobEndToEndMs', 'p50', 'LifecycleFn'), app('JobEndToEndMs', 'p95', 'LifecycleFn')],
      }),
      new cw.GraphWidget({
        title: 'API requests / 4xx / 5xx / latency p95', width: 12,
        left: [props.httpApi.metricCount(), props.httpApi.metricClientError(), props.httpApi.metricServerError()],
        right: [props.httpApi.metricLatency({ statistic: 'p95' })],
      }),
      new cw.GraphWidget({
        title: 'Stalled jobs / AI failures', width: 12,
        left: [app('StalledJobs', 'Maximum', 'StallCheckFn'), app('AiSuggestionFailures', 'Sum', 'ApiFn')],
      }),
    );
  }
}
