# Runs trace_parser on fixture files and checks key fields in the output.
function(check fmt input extra expect_list)
  execute_process(COMMAND ${PARSER} --format ${fmt} ${extra}
                  INPUT_FILE ${DATA}/${input}
                  OUTPUT_VARIABLE out RESULT_VARIABLE rc)
  if(NOT rc EQUAL 0)
    message(FATAL_ERROR "${fmt} exited ${rc}")
  endif()
  foreach(e IN LISTS expect_list)
    string(FIND "${out}" "${e}" pos)
    if(pos EQUAL -1)
      message(FATAL_ERROR "${fmt}: expected '${e}' in output:\n${out}")
    endif()
  endforeach()
endfunction()

check(perf-stat perf_stat.csv ""
  "\"event\":\"cycles\",\"value\":4012345678;\"status\":\"<not supported>\";\"rows\":4,\"not_counted\":1")
check(perf-stat perf_stat_interval.csv ""
  "\"t\":0.100183,\"event\":\"task-clock\",\"value\":98.12;\"rows\":3")
check(perf-sched perf_sched.txt "--pid;1234"
  "\"dur_ns\":150000,\"cpu_out\":2,\"cpu_in\":2,\"preempted\":true,\"by\":\"kworker/2:1\";\"preempted\":false;\"kind\":\"migration\",\"pid\":1234;\"offcpu\":2,\"preempted\":1,\"migrations\":1")
check(bpftrace-hist bpftrace_hist.txt ""
  "\"map\":\"@usecs\",\"lo\":1,\"hi\":2,\"count\":140;\"lo\":1024,\"hi\":2048,\"count\":2;\"buckets\":4")
