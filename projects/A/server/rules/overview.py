def overview(douyin_pending,maintenance_pending,spark_pending,spark_waiting=(),opportunities=(),wechat=(),unknown=()):
    union=set(douyin_pending)|set(maintenance_pending)|set(spark_pending)
    return {'action':sorted(union),'count':len(union),'waiting':sorted(set(spark_waiting)),
            'optional':sorted(set(opportunities)),'wechat':sorted(set(wechat)),'unknown':sorted(set(unknown))}
