<template>
  <Row>
    <Row type="flex" justify="start" class="header-container">
      <Col class="title"> Jurvis </Col>
      <Col class="desc">The least accurate prediction of football match</Col>
    </Row>
  </Row>
</template>
<script>
export default {
  name: 'BaseHeader',
  data() {
    return {
      msg: 'Welcome to Your Vue.js App',
    };
  },
};
</script>
<!-- Add "scoped" attribute to limit CSS to this component only -->
<style scoped lang="less">
.header-container {
  height: 60px;
  padding: 0 40px;
  .title {
    height: 60px;
    line-height: 60px;
    font-size: 24px;
    font-weight: 500;
  }
  .desc {
    line-height: 60px;
    font-size: 14px;
    font-weight: lighter;
    padding: 0 20px;
  }
}
</style>
